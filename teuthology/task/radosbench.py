"""
Rados benchmarking
"""
import contextlib
import logging

from ..orchestra import run
from teuthology import misc as teuthology

log = logging.getLogger(__name__)

@contextlib.contextmanager
def task(ctx, config):
    """
    Run radosbench

    The config should be as follows:

    radosbench:
        clients: [client list]
        time: <seconds to run>
        pool: <pool to use>
        unique_pool: use a unique pool, defaults to False
        erasure_code_profile:
          name: teuthologyprofile
        ec_pool: create ec pool, defaults to False

    example:

    tasks:
    - ceph:
    - radosbench:
        clients: [client.0]
        time: 360
    - interactive:
    """
    log.info('Beginning radosbench...')
    assert isinstance(config, dict), \
        "please list clients to run on"
    radosbench = {}

    testdir = teuthology.get_testdir(ctx)

    for role in config.get('clients', ['client.0']):
        assert isinstance(role, basestring)
        PREFIX = 'client.'
        assert role.startswith(PREFIX)
        id_ = role[len(PREFIX):]
        (remote,) = ctx.cluster.only(role).remotes.iterkeys()

        if config.get('ec_pool', False):
            erasure_code_profile = config.get('erasure_code_profile', {})
            erasure_code_profile_name = erasure_code_profile.get('name', False)
            ctx.manager.create_erasure_code_profile(erasure_code_profile_name, **erasure_code_profile)
        else:
            erasure_code_profile_name = False

        pool = 'data'
        if config.get('pool'):
            pool = config.get('pool')
            if pool is not 'data':
                ctx.manager.create_pool(pool, erasure_code_profile_name=erasure_code_profile_name)
        else:
            pool = ctx.manager.create_pool_with_unique_name(erasure_code_profile_name=erasure_code_profile_name)

        proc = remote.run(
            args=[
                "/bin/sh", "-c",
                " ".join(['adjust-ulimits',
                          'ceph-coverage',
                          '{tdir}/archive/coverage',
                          'rados',
                          '--name', role,
                          '-p' , pool,
                          'bench', str(config.get('time', 360)), 'write',
                          ]).format(tdir=testdir),
                ],
            logger=log.getChild('radosbench.{id}'.format(id=id_)),
            stdin=run.PIPE,
            wait=False
            )
        radosbench[id_] = proc

    try:
        yield
    finally:
        timeout = config.get('time', 360) * 5
        log.info('joining radosbench (timing out after %ss)', timeout)
        run.wait(radosbench.itervalues(), timeout=timeout)

        if pool is not 'data':
            ctx.manager.remove_pool(pool)
