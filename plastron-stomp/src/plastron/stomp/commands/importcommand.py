import io
import logging
from argparse import ArgumentTypeError
from typing import Generator, Any, Dict, Optional

from rdflib import URIRef

from plastron.jobs.importjob import ImportJobs, ImportConfig
from plastron.rdf import uri_or_curie
from plastron.repo import Repository
from plastron.stomp.messages import PlastronCommandMessage
from plastron.utils import datetimestamp

logger = logging.getLogger(__name__)


def get_access_uri(access) -> Optional[URIRef]:
    if access is None:
        return None
    try:
        return uri_or_curie(access)
    except ArgumentTypeError as e:
        raise RuntimeError(f'PlastronArg-access {e}')


def importcommand(
    repo: Repository,
    config: Dict[str, Any],
    message: PlastronCommandMessage,
) -> Generator[Any, None, Dict[str, Any]]:
    """
    Performs the import

    :param repo: the repository configuration
    :param config:
    :param message:
    """
    job_id = message.job_id

    # per-request options that are NOT saved to the config
    limit = message.args.get('limit', None)
    if limit is not None:
        limit = int(limit)
    message.body = message.body.encode('utf-8').decode('utf-8-sig')
    percentage = message.args.get('percent', None)
    validate_only = message.args.get('validate-only', False)
    resume = message.args.get('resume', False)
    import_file = io.StringIO(message.body)

    # options that are saved to the config
    job_config_args = {
        'job_id': job_id,
        'model': message.args.get('model'),
        'access': get_access_uri(message.args.get('access')),
        'member_of': message.args.get('member-of'),
        'container': message.args.get('relpath'),
        'binaries_location': message.args.get('binaries-location'),
    }

    if resume and job_id is None:
        raise RuntimeError('Resuming a job requires a job id')

    if job_id is None:
        # TODO: generate a more unique id? add in user and hostname?
        job_id = f"import-{datetimestamp()}"

    jobs = ImportJobs(directory=config.get('JOBS_DIR', 'jobs'))
    if resume:
        job = jobs.get_job(job_id=job_id)
        # update the config with any changes in this request
        job.update_config(job_config_args)
        job.ssh_private_key = config.get('SSH_PRIVATE_KEY', None)
    else:
        job = jobs.create_job(config=ImportConfig(**job_config_args))

    return job.run(
        repo=repo,
        import_file=import_file,
        limit=limit,
        percentage=percentage,
        validate_only=validate_only,
    )
