import logging
import requests
import os
import logging
from .config import config
from .misc import canonicalize_hostname

log = logging.getLogger(__name__)


def get_status(name):
    name = canonicalize_hostname(name, user=None)
    uri = os.path.join(config.lock_server, 'nodes', name, '')
    log.info("lockstatus::get_status uri = " + uri)
    response = requests.get(uri)
    success = response.ok
    if success:
        return response.json()
    log.warning(
        "Failed to query lock server for status of {name}".format(name=name))
    return None
