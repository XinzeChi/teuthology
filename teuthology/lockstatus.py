import logging
import requests
import os
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
    return None
