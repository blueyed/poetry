import sys

from typing import List
from typing import Union

from poetry.packages import Dependency
from poetry.packages import Locker
from poetry.packages import Package
from poetry.packages.constraints.generic_constraint import GenericConstraint
from poetry.puzzle import Solver
from poetry.puzzle.operations import Install
from poetry.puzzle.operations import Uninstall
from poetry.puzzle.operations import Update
from poetry.puzzle.operations.operation import Operation
from poetry.repositories import Pool
from poetry.repositories import Repository
from poetry.repositories.installed_repository import InstalledRepository
from poetry.semver.constraints import Constraint
from poetry.semver.version_parser import VersionParser

from .base_installer import BaseInstaller
from .pip_installer import PipInstaller


class Installer:

    def __init__(self,
                 io,
                 venv,
                 package,  # type: Package
                 locker,   # type: Locker
                 pool,     # type: Pool
                 installed=None  # type: (Union[InstalledRepository, None])
                 ):
        self._io = io
        self._venv = venv
        self._package = package
        self._locker = locker
        self._pool = pool

        self._dry_run = False
        self._update = False
        self._verbose = False
        self._write_lock = True
        self._dev_mode = True
        self._execute_operations = True

        self._whitelist = {}

        self._extras = []

        self._installer = self._get_installer()
        if installed is None:
            installed = self._get_installed()

        self._installed_repository = installed

    @property
    def installer(self):
        return self._installer

    def run(self):
        # Force update if there is no lock file present
        if not self._update and not self._locker.is_locked():
            self._update = True

        if self.is_dry_run():
            self.verbose(True)
            self._write_lock = False
            self._execute_operations = False

        local_repo = Repository()
        self._do_install(local_repo)

        return 0

    def dry_run(self, dry_run=True):  # type: (bool) -> Installer
        self._dry_run = dry_run

        return self

    def is_dry_run(self):  # type: () -> bool
        return self._dry_run

    def verbose(self, verbose=True):  # type: (bool) -> Installer
        self._verbose = verbose

        return self

    def is_verbose(self):  # type: () -> bool
        return self._verbose

    def dev_mode(self, dev_mode=True):  # type: (bool) -> Installer
        self._dev_mode = dev_mode

        return self

    def is_dev_mode(self):  # type: () -> bool
        return self._dev_mode

    def update(self, update=True):  # type: (bool) -> Installer
        self._update = update

        return self

    def is_updating(self):  # type: () -> bool
        return self._update

    def execute_operations(self, execute=True):  # type: (bool) -> Installer
        self._execute_operations = execute

        return self

    def whitelist(self, packages):  # type: (dict) -> Installer
        self._whitelist = packages

        return self

    def extras(self, extras):  # type: (list) -> Installer
        self._extras = extras

        return self

    def _do_install(self, local_repo):
        locked_repository = Repository()
        if self._update:
            if self._locker.is_locked():
                locked_repository = self._locker.locked_repository(True)

            # Checking extras
            for extra in self._extras:
                if extra not in self._package.extras:
                    raise ValueError(
                        'Extra [{}] is not specified.'.format(extra)
                    )

            self._io.writeln('<info>Updating dependencies</>')
            fixed = []

            # If the whitelist is enabled, packages not in it are fixed
            # to the version specified in the lock
            if self._whitelist:
                # collect packages to fixate from root requirements
                candidates = []
                for package in locked_repository.packages:
                    candidates.append(package)

                # fix them to the version in lock if they are not updateable
                for candidate in candidates:
                    to_fix = True
                    for require in self._whitelist.keys():
                        if require == candidate.name:
                            to_fix = False

                    if to_fix:
                        fixed.append(
                            Dependency(candidate.name, candidate.version)
                        )

            solver = Solver(
                self._package,
                self._pool,
                self._installed_repository,
                locked_repository,
                self._io
            )

            request = self._package.requires
            request += self._package.dev_requires

            ops = solver.solve(request, fixed=fixed)
        else:
            self._io.writeln('<info>Installing dependencies from lock file</>')

            locked_repository = self._locker.locked_repository(True)

            if not self._locker.is_fresh():
                self._io.writeln(
                    '<warning>'
                    'Warning: The lock file is not up to date with '
                    'the latest changes in pyproject.toml. '
                    'You may be getting outdated dependencies. '
                    'Run update to update them.'
                    '</warning>'
                )

            for extra in self._extras:
                if extra not in self._locker.lock_data.get('extras', {}):
                    raise ValueError(
                        'Extra [{}] is not specified.'.format(extra)
                    )

            # If we are installing from lock
            # Filter the operations by comparing it with what is
            # currently installed
            ops = self._get_operations_from_lock(locked_repository)

        self._populate_local_repo(local_repo, ops, locked_repository)

        # We need to filter operations so that packages
        # not compatible with the current system,
        # or optional and not requested, are dropped
        self._filter_operations(ops, local_repo)

        self._io.new_line()

        # Execute operations
        actual_ops = [op for op in ops if not op.skipped]
        if not actual_ops and (self._execute_operations or self._dry_run):
            self._io.writeln('Nothing to install or update')

        if actual_ops and (self._execute_operations or self._dry_run):
            installs = []
            updates = []
            uninstalls = []
            skipped = []
            for op in ops:
                if op.skipped:
                    skipped.append(op)
                    continue

                if op.job_type == 'install':
                    installs.append(
                        '{}:{}'.format(
                            op.package.pretty_name,
                            op.package.full_pretty_version
                        )
                    )
                elif op.job_type == 'update':
                    updates.append(
                        '{}:{}'.format(
                            op.target_package.pretty_name,
                            op.target_package.full_pretty_version
                        )
                    )
                elif op.job_type == 'uninstall':
                    uninstalls.append(op.package.pretty_name)

            self._io.new_line()
            self._io.writeln(
                'Package operations: '
                '<info>{}</> install{}, '
                '<info>{}</> update{}, '
                '<info>{}</> removal{}'
                '{}'.format(
                    len(installs), '' if len(installs) == 1 else 's',
                    len(updates), '' if len(updates) == 1 else 's',
                    len(uninstalls), '' if len(uninstalls) == 1 else 's',
                    ', <info>{}</> skipped'.format(
                        len(skipped)
                    ) if skipped and self.is_verbose() else ''
                )
            )

        # Writing lock before installing
        if self._update and self._write_lock:
            updated_lock = self._locker.set_lock_data(
                self._package,
                local_repo.packages
            )

            if updated_lock:
                self._io.writeln('')
                self._io.writeln('<info>Writing lock file</>')

        self._io.writeln('')
        for op in ops:
            self._execute(op)

    def _execute(self, operation):  # type: (Operation) -> None
        """
        Execute a given operation.
        """
        method = operation.job_type

        getattr(self, '_execute_{}'.format(method))(operation)

    def _execute_install(self, operation):  # type: (Install) -> None
        if operation.skipped:
            if self.is_verbose() and (self._execute_operations or self.is_dry_run()):
                self._io.writeln(
                    '  - Skipping <info>{}</> (<comment>{}</>) {}'.format(
                        operation.package.pretty_name,
                        operation.package.full_pretty_version,
                        operation.skip_reason
                    )
                )

            return

        if self._execute_operations or self.is_dry_run():
            self._io.writeln(
                '  - Installing <info>{}</> (<comment>{}</>)'.format(
                    operation.package.pretty_name,
                    operation.package.full_pretty_version
                )
            )

        if not self._execute_operations:
            return

        self._installer.install(operation.package)

    def _execute_update(self, operation):  # type: (Update) -> None
        source = operation.initial_package
        target = operation.target_package

        if operation.skipped:
            if self.is_verbose() and (self._execute_operations or self.is_dry_run()):
                self._io.writeln(
                    '  - Skipping <info>{}</> (<comment>{}</>) {}'.format(
                        target.pretty_name,
                        target.full_pretty_version,
                        operation.skip_reason
                    )
                )

            return

        if self._execute_operations or self.is_dry_run():
            self._io.writeln(
                '  - Updating <info>{}</> (<comment>{}</> -> <comment>{}</>)'
                .format(
                    target.pretty_name,
                    source.full_pretty_version,
                    target.full_pretty_version
                )
            )

        if not self._execute_operations:
            return

        self._installer.update(source, target)

    def _execute_uninstall(self, operation):  # type: (Uninstall) -> None
        if operation.skipped:
            if self.is_verbose() and (self._execute_operations or self.is_dry_run()):
                self._io.writeln(
                    '  - Not removing <info>{}</> (<comment>{}</>) {}'.format(
                        operation.package.pretty_name,
                        operation.package.full_pretty_version,
                        operation.skip_reason
                    )
                )

            return

        if self._execute_operations or self.is_dry_run():
            self._io.writeln(
                '  - Removing <info>{}</> (<comment>{}</>)'.format(
                    operation.package.pretty_name,
                    operation.package.full_pretty_version
                )
            )

        if not self._execute_operations:
            return

        self._installer.remove(operation.package)

    def _populate_local_repo(self, local_repo, ops, locked_repository):
        # Add all locked packages from the lock and go from there
        for package in locked_repository.packages:
            if not local_repo.has_package(package):
                local_repo.add_package(package)

        # Now, walk through all operations and add/remove/update accordingly
        for op in ops:
            if isinstance(op, Update):
                package = op.target_package
            else:
                package = op.package

            acted_on = False
            for pkg in local_repo.packages:
                if pkg.name == package.name:
                    # The package we operate on is in the local repo
                    if op.job_type == 'update':
                        if pkg.version == package.version:
                            break

                        local_repo.remove_package(pkg)
                        local_repo.add_package(op.target_package)
                    elif op.job_type == 'uninstall':
                        local_repo.remove_package(op.package)
                    else:
                        # Even though the package already exists
                        # in the lock file we will prefer the new one
                        # to force updates
                        local_repo.remove_package(pkg)
                        local_repo.add_package(package)

                    acted_on = True

            if not acted_on:
                if not local_repo.has_package(package):
                    local_repo.add_package(package)

    def _get_operations_from_lock(self,
                                  locked_repository  # type: Repository
                                  ):  # type: (...) -> List[Operation]
        installed_repo = self._installed_repository
        ops = []

        extra_packages = [
            p.name
            for p in self._get_extra_packages(locked_repository)
        ]
        for locked in locked_repository.packages:
            is_installed = False
            for installed in installed_repo.packages:
                if locked.name == installed.name:
                    is_installed = True
                    if locked.category == 'dev' and not self.is_dev_mode():
                        ops.append(Uninstall(locked))
                    elif locked.optional and locked.name not in extra_packages:
                        # Installed but optional and not requested in extras
                        ops.append(Uninstall(locked))
                    elif locked.version != installed.version:
                        ops.append(Update(
                            installed, locked
                        ))

            if not is_installed:
                # If it's optional and not in required extras
                # we do not install
                if locked.optional and locked.name not in extra_packages:
                    continue

                ops.append(Install(locked))

        return ops

    def _filter_operations(self,
                           ops,
                           repo
                           ):  # type: (List[Operation], Repository) -> None
        extra_packages = [p.name for p in
                          self._get_extra_packages(repo)]
        for op in ops:
            if isinstance(op, Update):
                package = op.target_package
            else:
                package = op.package

            if op.job_type == 'uninstall':
                continue

            parser = VersionParser()
            python = '.'.join([str(i) for i in self._venv.version_info[:3]])
            if 'python' in package.requirements:
                python_constraint = parser.parse_constraints(
                    package.requirements['python']
                )
                if not python_constraint.matches(Constraint('=', python)):
                    # Incompatible python versions
                    op.skip('Not needed for the current python version')
                    continue

            if not package.python_constraint.matches(Constraint('=', python)):
                op.skip('Not needed for the current python version')
                continue

            if 'platform' in package.requirements:
                platform_constraint = GenericConstraint.parse(
                    package.requirements['platform']
                )
                if not platform_constraint.matches(
                        GenericConstraint('=', sys.platform)
                ):
                    # Incompatible systems
                    op.skip('Not needed for the current platform')
                    continue

            if self._update:
                extras = {}
                for extra, deps in self._package.extras.items():
                    extras[extra] = [dep.name for dep in deps]
            else:
                extras = {}
                for extra, deps in self._locker.lock_data.get('extras', {}).items():
                    extras[extra] = [dep.lower() for dep in deps]

            # If a package is optional and not requested
            # in any extra we skip it
            if package.optional:
                if package.name not in extra_packages:
                    op.skip('Not required')

            # If the package is a dev package and dev packages
            # are not requests, we skip it
            if package.category == 'dev' and not self.is_dev_mode():
                op.skip('Dev dependencies not requested')

    def _get_extra_packages(self, repo):
        """
        Returns all packages required by extras.

        Maybe we just let the solver handle it?
        """
        if self._update:
            extras = {
                k: [d.name for d in v]
                for k, v in self._package.extras.items()
            }
        else:
            extras = self._locker.lock_data.get('extras', {})

        extra_packages = []
        for extra_name, packages in extras.items():
            if extra_name not in self._extras:
                continue

            extra_packages += [Dependency(p, '*') for p in packages]

        def _extra_packages(packages):
            pkgs = []
            for package in packages:
                for pkg in repo.packages:
                    if pkg.name == package.name:
                        pkgs.append(package)
                        pkgs += _extra_packages(pkg.requires)

                        break

            return pkgs

        return _extra_packages(extra_packages)

    def _get_installer(self):  # type: () -> BaseInstaller
        return PipInstaller(self._venv, self._io)

    def _get_installed(self):  # type: () -> InstalledRepository
        return InstalledRepository.load(self._venv)
