import os
import re

from poetry.version.requirements import Requirement

from .dependency import Dependency
from .file_dependency import FileDependency
from .locker import Locker
from .package import Package
from .utils.link import Link
from .utils.utils import convert_markers
from .utils.utils import group_markers
from .utils.utils import is_archive_file
from .utils.utils import is_installable_dir
from .utils.utils import is_url
from .utils.utils import path_to_url
from .utils.utils import strip_extras
from .vcs_dependency import VCSDependency


def dependency_from_pep_508(name):
    req = Requirement(name)

    if req.marker:
        markers = convert_markers(req.marker.markers)
    else:
        markers = {}

    name = req.name
    path = os.path.normpath(os.path.abspath(name))
    link = None

    if is_url(name):
        link = Link(name)
    else:
        p, extras = strip_extras(path)
        if (os.path.isdir(p) and
                (os.path.sep in name or name.startswith('.'))):

            if not is_installable_dir(p):
                raise ValueError(
                    "Directory {!r} is not installable. File 'setup.py' "
                    "not found.".format(name)
                )
            link = Link(path_to_url(p))
        elif is_archive_file(p):
            link = Link(path_to_url(p))

    # it's a local file, dir, or url
    if link:
        # Handle relative file URLs
        if link.scheme == 'file' and re.search(r'\.\./', link.url):
            link = Link(
                path_to_url(os.path.normpath(os.path.abspath(link.path)))
            )
        # wheel file
        if link.is_wheel:
            m = re.match(
                '^(?P<namever>(?P<name>.+?)-(?P<ver>\d.*?))',
                link.filename
            )
            if not m:
                raise ValueError('Invalid wheel name: {}'.format(link.filename))

            name = m.group('name')
            version = m.group('ver')
            dep = Dependency(name, version)
        else:
            name = link.egg_fragment

            if link.scheme == 'git':
                dep = VCSDependency(name, 'git', link.url_without_fragment)
            else:
                dep = Dependency(name, '*')
    else:
        if req.pretty_constraint:
            constraint = req.constraint
        else:
            constraint = '*'

        dep = Dependency(name, constraint)

    if 'extra' in markers:
        # If we have extras, the dependency is optional
        dep.deactivate()

        for or_ in markers['extra']:
            for _, extra in or_:
                dep.extras.append(extra)

    if 'python_version' in markers:
        ors = []
        for or_ in markers['python_version']:
            ands = []
            for op, version in or_:
                # Expand python version
                if op == '==':
                    version = '~' + version
                    op = ''
                elif op == '!=':
                    version += '.*'
                elif op == 'in':
                    versions = []
                    for v in version.split(' '):
                        split = v.split('.')
                        if len(split) in [1, 2]:
                            split.append('*')
                            op = ''
                        else:
                            op = '=='

                        versions.append(op + '.'.join(split))

                    if versions:
                        ands.append(' || '.join(versions))

                    continue

                ands.append('{}{}'.format(op, version))

            ors.append(' '.join(ands))

        dep.python_versions = ' || '.join(ors)

    if 'sys_platform' in markers:
        ors = []
        for or_ in markers['sys_platform']:
            ands = []
            for op, platform in or_:
                if op == '==':
                    op = ''
                elif op == 'in':
                    platforms = []
                    for v in platform.split(' '):
                        platforms.append(v)

                    if platforms:
                        ands.append(' || '.join(platforms))

                    continue

                ands.append('{}{}'.format(op, platform))

            ors.append(' '.join(ands))

        dep.platform = ' || '.join(ors)

    return dep
