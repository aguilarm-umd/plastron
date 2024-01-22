import dataclasses
import logging
import os
import re
import urllib.parse
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from os.path import basename
from pathlib import Path
from shutil import copyfileobj
from typing import Optional, Union, Any, IO, List, Generator, Dict, Iterable

import yaml
from rdflib import URIRef

from plastron.client import ClientError
from plastron.files import BinarySource, ZipFileSource, RemoteFileSource, HTTPFileSource, LocalFileSource
from plastron.jobs import JobConfigError, JobError, annotate_from_files
from plastron.jobs.imports.spreadsheet import LineReference, MetadataSpreadsheet, InvalidRow, Row
from plastron.models import get_model_class, ModelClassNotFoundError
from plastron.namespaces import umdaccess
from plastron.rdf.pcdm import File, PreservationMasterFile
from plastron.rdfmapping.resources import RDFResourceBase
from plastron.rdfmapping.validation import ValidationResultsDict, ValidationResult, ValidationSuccess, ValidationFailure
from plastron.repo import Repository, RepositoryError, ContainerResource
from plastron.repo.pcdm import PCDMObjectResource
from plastron.utils import datetimestamp, ItemLog
from plastron.validation import ValidationError

logger = logging.getLogger(__name__)
DROPPED_INVALID_FIELDNAMES = ['id', 'timestamp', 'title', 'uri', 'reason']
DROPPED_FAILED_FIELDNAMES = ['id', 'timestamp', 'title', 'uri', 'reason']


def is_run_dir(path: Path) -> bool:
    return path.is_dir() and re.match(r'^\d{14}$', path.name)


class ImportedItemStatus(Enum):
    CREATED = 'created'
    MODIFIED = 'modified'
    UNCHANGED = 'unchanged'


@dataclass
class ImportConfig:
    job_id: str
    model: Optional[str] = None
    access: Optional[str] = None
    member_of: Optional[str] = None
    container: Optional[str] = None
    binaries_location: Optional[str] = None
    extract_text_types: Optional[str] = None

    @classmethod
    def from_file(cls, filename: Union[str, Path]) -> 'ImportConfig':
        try:
            with open(filename) as file:
                config = yaml.safe_load(file)
        except FileNotFoundError as e:
            raise JobConfigError(f'Config file {filename} is missing') from e
        if config is None:
            raise JobConfigError(f'Config file {filename} is empty')
        for key, value in config.items():
            # catch any improperly serialized "None" values, and convert to None
            if value == 'None':
                config[key] = None
        return cls(**config)

    def save(self, filename: Union[str, Path]):
        config = {k: str(v) if v is not None else v for k, v in vars(self).items()}
        with open(filename, mode='w') as file:
            yaml.dump(data=config, stream=file)


class ImportJob:
    def __init__(self, job_id, jobs_dir, ssh_private_key: str = None):
        self.id = job_id
        # URL-escaped ID that can be used as a path segment on a filesystem or URL
        self.safe_id = urllib.parse.quote(job_id, safe='')
        self.dir = Path(jobs_dir) / self.safe_id
        self.config = ImportConfig(job_id=job_id)
        self._model_class = None

        # record of items that are successfully loaded
        completed_fieldnames = ['id', 'timestamp', 'title', 'uri', 'status']
        self.completed_log = ItemLog(self.dir / 'completed.log.csv', completed_fieldnames, 'id')

        self.ssh_private_key = ssh_private_key
        self.validation_reports = []

    def __str__(self):
        return self.id

    @property
    def config_filename(self) -> Path:
        return self.dir / 'config.yml'

    @property
    def metadata_filename(self) -> Path:
        return self.dir / 'source.csv'

    @property
    def exists(self) -> bool:
        return self.dir.is_dir()

    @property
    def model_class(self):
        if self._model_class is None:
            self._model_class = get_model_class(self.config.model)
        return self._model_class

    def load_config(self) -> ImportConfig:
        self.config = ImportConfig.from_file(self.config_filename)
        return self.config

    def store_metadata_file(self, input_file: IO):
        with open(self.metadata_filename, mode='w') as file:
            copyfileobj(input_file, file)
            logger.debug(f"Copied input file {getattr(input_file, 'name', '<>')} to {file.name}")

    def complete(self, item: RDFResourceBase, line_reference: LineReference, status: ImportedItemStatus):
        # write to the completed item log
        self.completed_log.append({
            'id': getattr(item, 'identifier', str(line_reference)),
            'timestamp': datetimestamp(digits_only=False),
            'title': getattr(item, 'title', ''),
            'uri': getattr(item, 'uri', ''),
            'status': status.value
        })

    def new_run(self) -> 'ImportRun':
        return ImportRun(self)

    def get_run(self, timestamp: Optional[str] = None) -> 'ImportRun':
        if timestamp is None:
            # get the latest run
            return self.latest_run()
        else:
            return ImportRun(self).load(timestamp)

    @property
    def runs(self) -> List[str]:
        return sorted((d.name for d in filter(is_run_dir, self.dir.iterdir())), reverse=True)

    def latest_run(self) -> Optional['ImportRun']:
        try:
            return ImportRun(self).load(self.runs[0])
        except IndexError:
            return None

    def get_metadata(self) -> MetadataSpreadsheet:
        return MetadataSpreadsheet(metadata_filename=self.metadata_filename, model_class=self.model_class)

    def start(
            self,
            repo: Repository,
            model: str,
            access: URIRef = None,
            member_of: URIRef = None,
            container: str = None,
            binaries_location: str = None,
            extract_text_types: str = None,
            **kwargs,
    ) -> Generator[Dict[str, Any], None, Dict[str, Any]]:
        config = {
            'model': model,
            'access': access,
            'member_of': member_of,
            'container': container,
            'binaries_location': binaries_location,
            'extract_text_types': extract_text_types,
        }
        # store the relevant config
        self.config = dataclasses.replace(self.config, **config)
        # if we are not resuming, make sure the directory exists
        os.makedirs(self.dir, exist_ok=True)
        self.config.save(self.config_filename)

        run = self.new_run()
        return run(repo=repo, **kwargs)

    def resume(self, repo: Repository, **kwargs) -> Generator[Dict[str, Any], None, Dict[str, Any]]:
        if not self.exists:
            raise RuntimeError(f'Cannot resume job "{self.id}": no such job directory: "{self.dir}"')

        logger.info(f'Resuming saved job {self.id}')
        try:
            # load stored config from the previous run of this job
            self.load_config()
        except FileNotFoundError:
            raise RuntimeError(f'Cannot resume job {self.id}: no config.yml found in {self.dir}')

        run = self.new_run()
        return run(repo=repo, **kwargs)

    @property
    def access(self) -> Optional[URIRef]:
        if self.config.access is not None:
            return URIRef[self.config.access]
        else:
            return None

    @property
    def member_of(self) -> Optional[URIRef]:
        if self.config.member_of is not None:
            return URIRef(self.config.member_of)
        else:
            return None

    @property
    def extract_text_types(self) -> List[str]:
        if self.config.extract_text_types is not None:
            return self.config.extract_text_types.split(',')
        else:
            return []

    def get_import_row(self, repo: Repository, row: Row):
        return ImportRow(self, repo, row)

    def get_source(self, base_location: str, path: str) -> BinarySource:
        """
        Get an appropriate BinarySource based on the type of ``base_location``.
        The following forms of ``base_location`` are recognized:

        * ``zip:<path to zipfile>``
        * ``sftp:<user>@<host>/<path to dir>``
        * ``http://<host>/<path to dir>``
        * ``zip+sftp:<user>@<host>/<path to zipfile>``
        * ``<local dir path>``

        :param base_location:
        :param path:
        :return:
        """
        if base_location.startswith('zip:'):
            return ZipFileSource(base_location[4:], path)
        elif base_location.startswith('sftp:'):
            return RemoteFileSource(
                location=os.path.join(base_location, path),
                ssh_options={'key_filename': self.ssh_private_key}
            )
        elif base_location.startswith('http:') or base_location.startswith('https:'):
            base_uri = base_location if base_location.endswith('/') else base_location + '/'
            return HTTPFileSource(base_uri + path)
        elif base_location.startswith('zip+sftp:'):
            return ZipFileSource(
                zip_file=base_location[4:],
                path=path,
                ssh_options={'key_filename': self.ssh_private_key}
            )
        else:
            # with no URI prefix, assume a local file path
            return LocalFileSource(localpath=os.path.join(base_location, path))

    def get_file(self, base_location: str, filename: str) -> File:
        """
        Get a file object for the given base_location and filename.

        Currently, if the file has an "image/tiff" MIME type, this method returns
        a :py:class:`plastron.pcdm.PreservationMasterFile`; otherwise it returns
        a basic :py:class:`plastron.pcdm.File`.

        :param base_location:
        :param filename:
        :return:
        """
        source = self.get_source(base_location, filename)

        # XXX: hardcoded image/tiff as the preservation master format
        # TODO: make preservation master format configurable per collection or job
        if source.mimetype() == 'image/tiff':
            file_class = PreservationMasterFile
        else:
            file_class = File

        return file_class.from_source(title=basename(filename), source=source)


class ImportRow:
    def __init__(self, job: ImportJob, repo: Repository, row: Row, validate_only: bool = False):
        self.job = job
        self.row = row
        self.repo = repo
        self.item = row.get_object(repo, read_from_repo=not validate_only)

    def __str__(self):
        return str(self.row.line_reference)

    def validate_item(self) -> ValidationResultsDict:
        """Validate the item for this import row, and check that all files
        listed are present."""
        try:
            results: ValidationResultsDict = self.item.validate()
        except ValidationError as e:
            raise RuntimeError(f'Unable to run validation: {e}') from e

        results['FILES'] = self.validate_files(self.row.filenames)
        results['ITEM_FILES'] = self.validate_files(self.row.item_filenames)

        return results

    def validate_files(self, filenames: Iterable[str]) -> ValidationResult:
        """Check that a file exists in the job's binaries location for each
        file name given."""
        missing_files = [
            name for name in filenames
            if not self.job.get_source(self.job.config.binaries_location, name).exists()
        ]
        if len(missing_files) == 0:
            return ValidationSuccess(
                prop=None,
                message='All files present',
            )
        else:
            return ValidationFailure(
                prop=None,
                message=f'Missing {len(missing_files)} files: {";".join(missing_files)}',
            )

    def update_repo(self) -> ImportedItemStatus:
        """Either creates a new item, updates an existing item, or does nothing
        to an existing item (if there are no changes)."""
        if self.item.uri.startswith('urn:uuid:'):
            resource = self.create_resource()
            logger.info(f'Created {resource.url}')
            return ImportedItemStatus.CREATED

        elif self.item.has_changes:
            # construct the SPARQL Update query if there are any deletions or insertions
            # then do a PATCH update of an existing item
            try:
                resource = self.repo[self.item.uri:PCDMObjectResource].read()
                resource.attach_description(self.item)
                resource.update()
            except RepositoryError as e:
                raise JobError(f'Updating item failed: {e}') from e

            logger.info(f'Updated {resource.url}')
            return ImportedItemStatus.MODIFIED

        else:
            logger.info(f'No changes found for "{self.item}" ({self.item.uri}); skipping')
            return ImportedItemStatus.UNCHANGED

    def create_resource(self) -> PCDMObjectResource:
        """Create a new item in the repository."""

        # if an item is new, don't construct a SPARQL Update query
        # instead, just create and update normally
        logger.debug('Creating a new item')
        # add the access class
        if self.job.access is not None:
            self.item.rdf_type.add(self.job.access)
        # add the collection membership
        if self.job.member_of is not None:
            self.item.member_of = self.job.member_of
        # set publication status
        if self.row.publish:
            self.item.rdf_type.add(umdaccess.Published)
        # set visibility
        if self.row.hidden:
            self.item.rdf_type.add(umdaccess.Hidden)

        if self.job.extract_text_types is not None:
            annotate_from_files(self.item, self.job.extract_text_types)

        container: ContainerResource = self.repo[self.job.config.container:ContainerResource]
        logger.debug(f"Creating resources in container: {container.path}")

        try:
            with self.repo.transaction():
                # create the main resource
                logger.debug(f'Creating main resource for "{self.item}"')
                resource = container.create_child(
                    resource_class=PCDMObjectResource,
                    description=self.item,
                )
                # add pages and files to those pages
                if self.row.has_files:
                    # update the file_groups to have a source
                    for file_group in self.row.file_groups.values():
                        for file in file_group.files:
                            file.source = self.job.get_source(self.job.config.binaries_location, file.name)
                    resource.create_page_sequence(self.row.file_groups)

                # item-level files
                if self.row.has_item_files:
                    for filename in self.row.item_filenames:
                        source = self.job.get_source(self.job.config.binaries_location, filename)
                        resource.create_file(source=source)

        except ClientError as e:
            raise JobError(self.job, f'Creating item failed: {e}', e.response.text) from e
        else:
            return resource


class ImportRun:
    """
    A single run of an import job. Records the logs of invalid and failed items (if any).
    """

    def __init__(self, job: ImportJob):
        self.job = job
        self.dir = None
        self.timestamp = None
        self._invalid_items = None
        self._failed_items = None

    def load(self, timestamp: str):
        """
        Load an existing import run by its timestamp.

        :param timestamp: should be 14 digits expressing YYYYMMDDHHMMSS
        :return:
        """
        self.timestamp = timestamp
        self.dir = self.job.dir / self.timestamp
        if not self.dir.is_dir():
            raise RuntimeError(f'Import run {self.timestamp} not found')
        return self

    @property
    def invalid_items(self) -> ItemLog:
        """
        Log of items that failed metadata validation during this import run.
        """
        if self._invalid_items is None:
            self._invalid_items = ItemLog(self.dir / 'dropped-invalid.log.csv', DROPPED_INVALID_FIELDNAMES, 'id')
        return self._invalid_items

    @property
    def failed_items(self) -> ItemLog:
        """
        Log of items that failed when loading into the repository during this import run.
        """
        if self._failed_items is None:
            self._failed_items = ItemLog(self.dir / 'dropped-failed.log.csv', DROPPED_FAILED_FIELDNAMES, 'id')
        return self._failed_items

    def __call__(self, *args, **kwargs):
        return self.run(*args, **kwargs)

    def run(
            self,
            repo: Repository,
            limit: int = None,
            percentage=None,
            validate_only: bool = False,
            import_file: IO = None,
    ) -> Generator[Dict[str, Any], None, Dict[str, Any]]:
        """Execute this import run. Returns a generator that yields a dictionary of
        current status after each item. The generator also returns a final status
        dictionary after the run has completed. This value can be captured using
        a delegating generator and Python's `yield from` syntax:

        ```python
        import_run = ImportRun(job)
        result = None

        # this is the delegated generator function
        def generator(repo):
            result = yield from import_run.run(repo)

        for status in generator(repo):
            print(status['count'], 'items completed')

        print('job status', result['type'])
        ```
        """
        if self.dir is not None:
            raise RuntimeError('Run completed, cannot start again')
        self.timestamp = datetimestamp()
        self.dir = self.job.dir / self.timestamp
        self.dir.mkdir(parents=True, exist_ok=True)
        start_time = datetime.now().timestamp()

        if percentage:
            logger.info(f'Loading {percentage}% of the total items')
        if validate_only:
            logger.info('Validation-only mode, skipping imports')

        # if an import file was provided, save that as the new CSV metadata file
        if import_file is not None:
            self.job.store_metadata_file(import_file)

        try:
            metadata = self.job.get_metadata()
        except ModelClassNotFoundError as e:
            raise RuntimeError(f'Model class {e.model_name} not found') from e
        except JobError as e:
            raise RuntimeError(str(e)) from e

        if metadata.has_binaries and self.job.config.binaries_location is None:
            raise RuntimeError('Must specify --binaries-location if the metadata has a FILES column')

        count = Counter(
            total_items=metadata.total,
            rows=0,
            errors=0,
            initially_completed_items=len(self.job.completed_log),
            files=0,
            valid_items=0,
            invalid_items=0,
            created_items=0,
            updated_items=0,
            unchanged_items=0,
            skipped_items=0,
        )
        logger.info(f'Found {count["initially_completed_items"]} completed items')

        for row in metadata.rows(limit=limit, percentage=percentage, completed=self.job.completed_log):
            if isinstance(row, InvalidRow):
                self.drop_invalid(item=None, line_reference=row.line_reference, reason=row.reason)
                continue

            logger.debug(f'Row data: {row.data}')
            import_row = ImportRow(self.job, repo, row, validate_only)

            # count the number of files referenced in this row
            count['files'] += len(row.filenames)

            # validate metadata and files
            validation = import_row.validate_item()
            if validation.ok:
                count['valid_items'] += 1
                logger.info(f'"{import_row}" is valid')
            else:
                # drop invalid items
                count['invalid_items'] += 1
                logger.warning(f'"{import_row}" is invalid, skipping')
                reasons = [f'{name} {result}' for name, result in validation.failures()]
                self.drop_invalid(
                    item=import_row.item,
                    line_reference=row.line_reference,
                    reason=f'Validation failures: {"; ".join(reasons)}'
                )
                continue

            if validate_only:
                # validation-only mode
                continue

            try:
                status = import_row.update_repo()
                self.complete(import_row.item, row.line_reference, status)
                if status == ImportedItemStatus.CREATED:
                    count['created_items'] += 1
                elif status == ImportedItemStatus.MODIFIED:
                    count['updated_items'] += 1
                elif status == ImportedItemStatus.UNCHANGED:
                    count['unchanged_items'] += 1
                    count['skipped_items'] += 1
                else:
                    raise RuntimeError(f'Unknown status "{status}" returned when importing "{import_row.item}"')
            except JobError as e:
                count['items_with_errors'] += 1
                logger.error(f'{import_row} import failed: {e}')
                self.drop_failed(import_row.item, row.line_reference, reason=str(e))

            # update the status
            now = datetime.now().timestamp()
            yield {
                'time': {
                    'started': start_time,
                    'now': now,
                    'elapsed': now - start_time
                },
                'count': count,
            }

        if validate_only:
            # validate phase
            if count['invalid_items'] == 0:
                result_type = 'validate_success'
            else:
                result_type = 'validate_failed'
        else:
            # import phase
            if len(self.job.completed_log) == count['total_items']:
                result_type = 'import_complete'
            else:
                result_type = 'import_incomplete'

        return {
            'type': result_type,
            'validation': self.job.validation_reports,
            'count': count,
        }

    def drop_failed(self, item, line_reference, reason=''):
        """
        Add the item to the log of failed items for this run.

        :param item: the failed item
        :param line_reference: string in the form <filename>:<line number>
        :param reason: cause of the failure; usually the message from the underlying exception
        :return:
        """
        logger.warning(
            f'Dropping failed {line_reference} from import job "{self.job}" run {self.timestamp}: {reason}'
        )
        self.failed_items.append({
            'id': getattr(item, 'identifier', line_reference),
            'timestamp': datetimestamp(digits_only=False),
            'title': getattr(item, 'title', ''),
            'uri': getattr(item, 'uri', ''),
            'reason': reason
        })

    def drop_invalid(self, item, line_reference, reason=''):
        """
        Add the item to the log of invalid items for this run.

        :param item: the invalid item
        :param line_reference: string in the form <filename>:<line number>
        :param reason: validation failure message(s)
        :return:
        """
        logger.warning(
            f'Dropping invalid {line_reference} from import job "{self.job}" run {self.timestamp}: {reason}'
        )
        self.invalid_items.append({
            'id': getattr(item, 'identifier', line_reference),
            'timestamp': datetimestamp(digits_only=False),
            'title': getattr(item, 'title', ''),
            'uri': getattr(item, 'uri', ''),
            'reason': reason
        })

    def complete(self, item, line_reference, status):
        """
        Delegates to the `ImportJob.complete()` method.

        :param item:
        :param line_reference:
        :param status:
        :return:
        """
        self.job.complete(item, line_reference, status)
