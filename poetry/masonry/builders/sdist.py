# -*- coding: utf-8 -*-
import os
import re
import tarfile

from collections import defaultdict
from copy import copy
from gzip import GzipFile
from io import BytesIO
from posixpath import join as pjoin
from pprint import pformat
from typing import List

from poetry.packages import Dependency
from poetry.utils._compat import Path
from poetry.utils._compat import encode
from poetry.utils._compat import to_str

from ..utils.helpers import normalize_file_permissions

from .builder import Builder


SETUP = """\
# -*- coding: utf-8 -*-
from distutils.core import setup

{before}
setup_kwargs = {{
    'name': {name!r},
    'version': {version!r},
    'description': {description!r},
    'long_description': {long_description!r},
    'author': {author!r},
    'author_email': {author_email!r},
    'url': {url!r},
    {extra}
}}
{after}

setup(**setup_kwargs)
"""


PKG_INFO = """\
Metadata-Version: 2.1
Name: {name}
Version: {version}
Summary: {summary}
Home-page: {home_page}
Author: {author}
Author-email: {author_email}
"""


class SdistBuilder(Builder):

    def build(self, target_dir=None):  # type: (Path) -> Path
        self._io.writeln(' - Building <info>sdist</info>')
        if target_dir is None:
            target_dir = self._path / 'dist'

        if not target_dir.exists():
            target_dir.mkdir(parents=True)

        target = target_dir / '{}-{}.tar.gz'.format(
            self._package.pretty_name, self._package.version
        )
        gz = GzipFile(target.as_posix(), mode='wb')
        tar = tarfile.TarFile(target.as_posix(), mode='w', fileobj=gz,
                              format=tarfile.PAX_FORMAT)

        try:
            tar_dir = '{}-{}'.format(
                self._package.pretty_name, self._package.version
            )

            files_to_add = self.find_files_to_add(exclude_build=False)

            for relpath in files_to_add:
                path = self._path / relpath
                tar_info = tar.gettarinfo(
                    str(path),
                    arcname=pjoin(tar_dir, str(relpath))
                )
                tar_info = self.clean_tarinfo(tar_info)

                if tar_info.isreg():
                    with path.open('rb') as f:
                        tar.addfile(tar_info, f)
                else:
                    tar.addfile(tar_info)  # Symlinks & ?

            setup = self.build_setup()
            tar_info = tarfile.TarInfo(pjoin(tar_dir, 'setup.py'))
            tar_info.size = len(setup)
            tar.addfile(tar_info, BytesIO(setup))

            pkg_info = self.build_pkg_info()

            tar_info = tarfile.TarInfo(pjoin(tar_dir, 'PKG-INFO'))
            tar_info.size = len(pkg_info)
            tar.addfile(tar_info, BytesIO(pkg_info))
        finally:
            tar.close()
            gz.close()

        self._io.writeln(' - Built <fg=cyan>{}</>'.format(target.name))

        return target

    def build_setup(self):  # type: () -> bytes
        before, extra, after = [], [], []

        # If we have a build script, use it
        if self._package.build:
            after += [
                'from {} import *'.format(self._package.build.split('.')[0]),
                'build(setup_kwargs)'
            ]

        if self._module.is_package():
            packages, package_data = self.find_packages(
                self._module.path.as_posix()
            )
            before.append("packages = \\\n{}\n".format(pformat(sorted(packages))))
            before.append("package_data = \\\n{}\n".format(pformat(package_data)))
            extra.append("'packages': packages,")
            extra.append("'package_data': package_data,")
        else:
            extra.append("'py_modules': {!r},".format(self._module.name))

        dependencies, extras = self.convert_dependencies(
            self._package,
            self._package.requires
        )
        if dependencies:
            before.append("install_requires = \\\n{}\n".format(pformat(dependencies)))
            extra.append("'install_requires': install_requires,")

        if extras:
            before.append("extras_require = \\\n{}\n".format(pformat(extras)))
            extra.append("'extras_require': extras_require,")

        entry_points = self.convert_entry_points()
        if entry_points:
            before.append("entry_points = \\\n{}\n".format(pformat(entry_points)))
            extra.append("'entry_points': entry_points,")

        if self._package.python_versions != '*':
            python_requires = self._meta.requires_python

            extra.append("'python_requires': {!r},".format(python_requires))

        return encode(SETUP.format(
            before='\n'.join(before),
            name=to_str(self._meta.name),
            version=to_str(self._meta.version),
            description=to_str(self._meta.summary),
            long_description=to_str(self._meta.description),
            author=to_str(self._meta.author),
            author_email=to_str(self._meta.author_email),
            url=to_str(self._meta.home_page),
            extra='\n    '.join(extra),
            after='\n'.join(after)
        ))

    def build_pkg_info(self):
        pkg_info = PKG_INFO.format(
            name=self._meta.name,
            version=self._meta.version,
            summary=self._meta.summary,
            home_page=self._meta.home_page,
            author=to_str(self._meta.author),
            author_email=to_str(self._meta.author_email),
        )

        if self._meta.keywords:
            pkg_info += "Keywords: {}\n".format(self._meta.keywords)

        if self._meta.requires_python:
            pkg_info += 'Requires-Python: {}\n'.format(
                self._meta.requires_python
            )

        for classifier in self._meta.classifiers:
            pkg_info += 'Classifier: {}\n'.format(classifier)

        for extra in sorted(self._meta.provides_extra):
            pkg_info += 'Provides-Extra: {}\n'.format(extra)

        for dep in sorted(self._meta.requires_dist):
            pkg_info += 'Requires-Dist: {}\n'.format(dep)

        return encode(pkg_info)

    @classmethod
    def find_packages(cls, path):
        """
        Discover subpackages and data.

        It also retrieve necessary files
        """
        pkgdir = os.path.normpath(path)
        pkg_name = os.path.basename(pkgdir)
        pkg_data = defaultdict(list)
        # Undocumented distutils feature:
        # the empty string matches all package names
        pkg_data[''].append('*')
        packages = [pkg_name]
        subpkg_paths = set()

        def find_nearest_pkg(rel_path):
            parts = rel_path.split(os.sep)
            for i in reversed(range(1, len(parts))):
                ancestor = '/'.join(parts[:i])
                if ancestor in subpkg_paths:
                    pkg = '.'.join([pkg_name] + parts[:i])
                    return pkg, '/'.join(parts[i:])

            # Relative to the top-level package
            return pkg_name, rel_path

        for path, dirnames, filenames in os.walk(pkgdir, topdown=True):
            if os.path.basename(path) == '__pycache__':
                continue

            from_top_level = os.path.relpath(path, pkgdir)
            if from_top_level == '.':
                continue

            is_subpkg = '__init__.py' in filenames
            if is_subpkg:
                subpkg_paths.add(from_top_level)
                parts = from_top_level.split(os.sep)
                packages.append('.'.join([pkg_name] + parts))
            else:
                pkg, from_nearest_pkg = find_nearest_pkg(from_top_level)
                pkg_data[pkg].append(pjoin(from_nearest_pkg, '*'))

        # Sort values in pkg_data
        pkg_data = {k: sorted(v) for (k, v) in pkg_data.items()}

        return sorted(packages), pkg_data

    @classmethod
    def convert_dependencies(cls,
                             package,      # type: Package
                             dependencies  # type: List[Dependency]
                             ):
        main = []
        extras = defaultdict(list)
        req_regex = re.compile('^(.+) \((.+)\)$')

        for dependency in dependencies:
            if dependency.is_optional():
                for extra_name, reqs in package.extras.items():
                    for req in reqs:
                        if req.name == dependency.name:
                            requirement = to_str(
                                dependency.to_pep_508(with_extras=False)
                            )
                            if ';' in requirement:
                                requirement, conditions = requirement.split(';')

                                requirement = requirement.strip()
                                if req_regex.match(requirement):
                                    requirement = req_regex.sub('\\1\\2',
                                                                requirement.strip())

                                extras[extra_name + ':' + conditions.strip()].append(requirement)

                                continue

                            requirement = requirement.strip()
                            if req_regex.match(requirement):
                                requirement = req_regex.sub('\\1\\2',
                                                            requirement.strip())
                            extras[extra_name].append(requirement)
                continue

            requirement = to_str(dependency.to_pep_508())
            if ';' in requirement:
                requirement, conditions = requirement.split(';')

                requirement = requirement.strip()
                if req_regex.match(requirement):
                    requirement = req_regex.sub('\\1\\2', requirement.strip())

                extras[':' + conditions.strip()].append(requirement)

                continue

            requirement = requirement.strip()
            if req_regex.match(requirement):
                requirement = req_regex.sub('\\1\\2', requirement.strip())

            main.append(requirement)

        return main, dict(extras)

    @classmethod
    def clean_tarinfo(cls, tar_info):
        """
        Clean metadata from a TarInfo object to make it more reproducible.

            - Set uid & gid to 0
            - Set uname and gname to ""
            - Normalise permissions to 644 or 755
            - Set mtime if not None
        """
        ti = copy(tar_info)
        ti.uid = 0
        ti.gid = 0
        ti.uname = ''
        ti.gname = ''
        ti.mode = normalize_file_permissions(ti.mode)
        
        return ti
