import logging
import yaml
from plastron import pcdm
from plastron.exceptions import RESTAPIException, FailureException
from plastron.http import Transaction

logger = logging.getLogger(__name__)


def configure_cli(subparsers):
    parser = subparsers.add_parser(
        name='mkcol',
        description='Create a PCDM Collection in the repository'
    )
    parser.add_argument(
        '-n', '--name',
        help='Name of the collection.',
        action='store',
        required=True
    )
    # if given, will write the collection URI to it
    parser.add_argument(
        '-b', '--batch',
        help='Path to batch configuration file.',
        action='store'
    )
    parser.set_defaults(cmd_name='mkcol')


class Command:
    def __call__(self, fcrepo, args):
        with Transaction(fcrepo) as txn:
            try:
                collection = pcdm.Collection()
                collection.title = args.name
                collection.create_object(fcrepo)
                collection.update_object(fcrepo)
                txn.commit()

            except RESTAPIException as e:
                logger.error(f'Error in collection creation: {e}')
                raise FailureException()

        if args.batch is not None:
            with open(args.batch, 'r') as batchconfig:
                batch = yaml.safe_load(batchconfig)
                batch['COLLECTION'] = str(collection.uri)
            with open(args.batch, 'w') as batchconfig:
                yaml.dump(batch, batchconfig, default_flow_style=False)
