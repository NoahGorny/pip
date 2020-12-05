from __future__ import absolute_import

import json
import logging
import sys
import textwrap
from collections import OrderedDict

from pip._vendor import pkg_resources, requests
from pip._vendor.packaging.utils import canonicalize_name
from pip._vendor.packaging.version import parse as parse_version

# NOTE: XMLRPC Client is not annotated in typeshed as on 2017-07-17, which is
#       why we ignore the type on this import
from pip._vendor.six.moves import xmlrpc_client  # type: ignore
from pip._vendor.six.moves.urllib import parse as urllib_parse

from pip._internal.cli.base_command import Command
from pip._internal.cli.req_command import SessionCommandMixin
from pip._internal.cli.status_codes import NO_MATCHES_FOUND, SUCCESS
from pip._internal.exceptions import CommandError, NetworkConnectionError
from pip._internal.models.index import PyPI
from pip._internal.network.utils import raise_for_status
from pip._internal.network.xmlrpc import PipXmlrpcTransport
from pip._internal.utils.compat import get_terminal_size
from pip._internal.utils.logging import indent_log
from pip._internal.utils.misc import get_distribution, write_output
from pip._internal.utils.typing import MYPY_CHECK_RUNNING

if MYPY_CHECK_RUNNING:
    from optparse import Values
    from typing import Dict, Iterable, List, Optional

    from typing_extensions import TypedDict
    TransformedHit = TypedDict(
        'TransformedHit',
        {'name': str, 'summary': str, 'versions': List[str]},
    )

logger = logging.getLogger(__name__)


class SearchCommand(Command, SessionCommandMixin):
    """Search for PyPI packages whose name or summary contains <query>."""

    usage = """
      %prog [options] <query>"""
    ignore_require_venv = True

    def add_options(self):
        # type: () -> None
        self.cmd_opts.add_option(
            '-i', '--index',
            dest='index',
            metavar='URL',
            default=PyPI.pypi_url,
            help='Base URL of Python Package Index (default %default)')

        self.cmd_opts.add_option(
            '-l', '--list-all-versions',
            dest='list_all_versions',
            action='store_true',
            default=False,
            help='List all available versions of the specific package name')

        self.parser.insert_option_group(0, self.cmd_opts)

    def get_all_package_versions(self, query, options):
        # type: (List[str], Values) -> List[Dict[str, str]]
        canonized_query = [canonicalize_name(q) for q in query]

        result_hits = []  # type: List[Dict[str, str]]
        for q in canonized_query:
            try:
                package_data = self.get_pypi_json(options, q)
            except (requests.ConnectionError,
                    requests.Timeout,
                    NetworkConnectionError) as exc:
                exc = str(exc)
                if '404' in exc:
                    err = 'Got 404 error, make sure the package name is valid.'
                else:
                    err = exc
                logger.error(
                    'Failed to fetch data of package "%s": %s', q, err)
                return result_hits

            versions = package_data.get('releases', {})  # type: Dict[str, str]

            info = package_data.get('info', {})  # type: Dict[str, str]
            for version in versions:
                result_hits.append(
                    {'name': info.get('name', ''),
                     'summary': info.get('summary', ''),
                     'version': version}
                )
        return result_hits

    def run(self, options, args):
        # type: (Values, List[str]) -> int
        if not args:
            raise CommandError('Missing required argument (search query).')
        query = args
        if options.list_all_versions:
            pypi_hits = self.get_all_package_versions(query, options)
        else:
            pypi_hits = self.search(query, options)

        hits = transform_hits(pypi_hits)

        terminal_width = None
        if sys.stdout.isatty():
            terminal_width = get_terminal_size()[0]

        print_results(
            hits, options.list_all_versions, terminal_width=terminal_width)
        if pypi_hits:
            return SUCCESS
        return NO_MATCHES_FOUND

    def get_pypi(self, options):
        # type: (Values) -> xmlrpc_client.ServerProxy
        index_url = options.index

        session = self.get_default_session(options)

        transport = PipXmlrpcTransport(index_url, session)
        return xmlrpc_client.ServerProxy(index_url, transport)

    def get_pypi_json(self, options, query):
        # type: (Values, str) -> Dict[str, Dict[str, str]]
        resp = requests.get(
                urllib_parse.urljoin(
                    options.index, '{}/{}/{}'.format('pypi', query, 'json')))
        raise_for_status(resp)
        return json.loads(resp.content.decode('utf-8'))

    def search(self, query, options):
        # type: (List[str], Values) -> List[Dict[str, str]]
        pypi = self.get_pypi(options)
        hits = pypi.search({'name': query, 'summary': query}, 'or')
        return hits


def transform_hits(hits):
    # type: (List[Dict[str, str]]) -> List[TransformedHit]
    """
    The list from pypi is really a list of versions. We want a list of
    packages with the list of versions stored inline. This converts the
    list from pypi into one we can use.
    """
    packages = OrderedDict()  # type: OrderedDict[str, TransformedHit]
    for hit in hits:
        name = hit['name']
        summary = hit['summary']
        version = hit['version']

        if name not in packages.keys():
            packages[name] = {
                'name': name,
                'summary': summary,
                'versions': [version],
            }
        else:
            packages[name]['versions'].append(version)

            # if this is the highest version, replace summary and score
            if version == highest_version(packages[name]['versions']):
                packages[name]['summary'] = summary

    return list(packages.values())


def print_results(
    hits,  # type: List[TransformedHit]
    list_all_versions=False,  # type: Optional[bool]
    name_column_width=None,  # type: Optional[int]
    terminal_width=None  # type: Optional[int]
):
    # type: (...) -> None
    if not hits:
        return
    if name_column_width is None:
        name_column_width = max([
            len(hit['name']) + len(highest_version(hit.get('versions', ['-'])))
            for hit in hits
        ]) + 4

    installed_packages = [p.project_name for p in pkg_resources.working_set]
    for hit in hits:
        name = hit['name']
        summary = hit['summary'] or ''
        versions = hit.get('versions', ['-'])
        latest = highest_version(versions)
        if terminal_width is not None:
            target_width = terminal_width - name_column_width - 5
            if target_width > 10:
                # wrap and indent summary to fit terminal
                summary_lines = textwrap.wrap(summary, target_width)
                summary = ('\n' + ' ' * (name_column_width + 3)).join(
                    summary_lines)

        line = '{name_latest:{name_column_width}} - {summary}'.format(
            name_latest='{name} ({latest})'.format(**locals()),
            **locals())
        try:
            write_output(line)
            if list_all_versions:
                write_output('Available versions: {}'.format(
                    _format_versions(versions)))
            if name in installed_packages:
                dist = get_distribution(name)
                assert dist is not None
                with indent_log():
                    if dist.version == latest:
                        write_output('INSTALLED: %s (latest)', dist.version)
                    else:
                        write_output('INSTALLED: %s', dist.version)
                        if parse_version(latest).pre:
                            write_output('LATEST:    %s (pre-release; install'
                                         ' with "pip install --pre")', latest)
                        else:
                            write_output('LATEST:    %s', latest)
        except UnicodeEncodeError:
            pass


def _format_versions(versions):
    # type: (Iterable[str]) -> str
    return ", ".join(sorted(
        {c for c in versions},
        key=parse_version,
        reverse=True,
    )) or "none"


def highest_version(versions):
    # type: (List[str]) -> str
    return max(versions, key=parse_version)
