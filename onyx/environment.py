from datetime import datetime
import importlib
import logging
import logging.handlers
import ujson

from flask import Flask
from mock import Mock
from statsd import StatsClient
import geoip2.database
import gevent
import os

class EnvironmentUninitializedError(Exception):
    pass


class Environment(object):

    def __init__(self, config_filename):
        self.__config_filename = config_filename
        self.config = self.load_config_obj(config_filename)
        self.__application = None
        self.__geoip_db = None
        self.statsd = None
        self.init()
        if not hasattr(Environment, "_instance"):
            Environment._instance = self

    def load_config_obj(self, obj_name):
        tokens = obj_name.split(".")
        module_name = ".".join(tokens[:-1])
        class_name = tokens[-1]
        module = importlib.import_module(module_name)
        return getattr(module, class_name)

    @property
    def is_debug(self):
        return self.config.DEBUG

    @property
    def is_test(self):
        return self.config.ENVIRONMENT == "test"

    @property
    def is_development(self):
        return self.config.ENVIRONMENT == "dev"

    @property
    def is_production(self):
        return self.config.ENVIRONMENT == "prod"

    @property
    def application(self):
        if not self.__application:
            raise EnvironmentUninitializedError("Cannot obtain application without initializing environment")
        return self.__application

    def __setup_loggers(self):
        """
        Setup and return a loggers configured for offline processing
        """
        loggers = {}
        for name, settings in self.config.LOG_HANDLERS.iteritems():
            internal_name = "onyx-{0}".format(name)

            handler = settings['handler'](**settings['params'])
            if 'format' in settings:
                handler.setFormatter(logging.Formatter(settings['format']))

            logger = logging.getLogger(internal_name)
            logger.setLevel(settings['level'])
            logger.addHandler(handler)
            loggers[internal_name] = logger
        return loggers

    def log_dict(self, name, message, action=None, **kwargs):
        """
        Special formatter that logs messages in a format log parsers expects.
        Requires a dict input
        """
        level = kwargs.pop('level', logging.INFO)
        internal_name = "onyx-{0}".format(name)
        logger = self.__loggers.get(internal_name, self.__loggers['onyx-console'])

        from onyx.utils import unix_time_millis
        now = datetime.utcnow()
        if action:
            message['action'] = action
        message['date'] = now.date().isoformat()
        message['timestamp'] = int(unix_time_millis(now))

        msg = ujson.dumps(message)
        if self.config.LOG_HANDLERS[name]['handler'] == logging.handlers.SysLogHandler:
            # in syslog, message starts after first colon
            msg = ":{0}".format(msg)
        logger.log(level, msg, **kwargs)

    def init(self):
        """
        Start point for application configuration
        """
        ### logging
        if self.is_test:
            self.log_dict = Mock()
            self.statsd = Mock()
        else:
            self.__loggers = self.__setup_loggers()
            self.statsd = StatsClient(**self.config.STATSD)

        # Application server setup
        app = Flask('onyx')
        app.config.from_object(self.__config_filename)

        if app.config['ENVIRONMENT'] not in app.config['STATIC_ENABLED_ENVS']:
            app.config['STATIC_FOLDER'] = None
        self.__application = app

        # spawn off the tile index reader
        if not self.is_test:
            gevent.spawn(_read_tile_index_loop, self)

        return app

    @property
    def geoip_db(self):
        if not self.__geoip_db:
            print self.config.GEO_DB_FILE
            self.__geoip_db = geoip2.database.Reader(self.config.GEO_DB_FILE)
        return self.__geoip_db

    @classmethod
    def instance(cls, config=None):
        if hasattr(Environment, "_instance") and config is None:
            return Environment._instance
        elif config is not None:
            return Environment(config)

        raise EnvironmentUninitializedError("Cannot obtain instance if uninitialized")


def _read_tile_index_loop(env):
    """wait for 15 minutes (greenlet), then open tile index file and replace LINKS_LOCALIZATIONS"""
    try:
        gevent.sleep(15 * 60)
        with open(os.path.join(env.config.TILE_INDEX_DIR, env.config.TILE_INDEX_FILE), "r") as fp:
            data = fp.readall()
            env.config.LINKS_LOCALIZATIONS = ujson.decode(data)
    except Exception as e:
        pass
        # TODO: do we have a log for error messages somewhere?



