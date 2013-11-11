#!/usr/bin/python -tt

import sys
import repo_db
from check_sources import OpenShiftCheckSources
from itertools import chain
from yum import Errors
import logging

OSE_PRIORITY = 10
RHEL_PRIORITY = 20
JBOSS_PRIORITY = 30
OTHER_PRIORITY = 40

UNKNOWN, RHSM, RHN = ('unknown', 'rhsm', 'rhn')
SUBS_NAME = {'unknown': '', 'rhsm': 'Red Hat Subscription Manager',
             'rhn': 'RHN Classic or RHN Satellite'}
VALID_SUBS = SUBS_NAME.keys()[1:]
ATTACH_ENTITLEMENTS_URL = 'https://access.redhat.com/site/articles/522923'
VALID_OO_VERSIONS = ['1.2', '2.0']
VALID_ROLES = ['node', 'broker', 'client', 'node-eap']

def flatten_uniq(llist):
    """Flatten nested iterables and filter result for uniqueness
    """
    return list(set(chain.from_iterable(llist)))

class UnrecoverableYumError(Exception):
    pass

class OpenShiftAdminCheckSources:
    pri_header = False
    pri_resolve_header = False
    problem = False
    resolved_repos = {}
    committed_resolved_repos = {}

    def __init__(self, opts, opt_parser):
        self.opts = opts
        self.opt_parser = opt_parser
        self._setup_logger()
        self.oscs = OpenShiftCheckSources()
        if not self.opts.subscription:
            self.opts.subscription = UNKNOWN
        else:
            self.opts.subscription = self.opts.subscription.lower()
        if self.opts.repo_config:
            self.rdb = repo_db.RepoDB(file(self.opts.repo_config),
                                      user_repos_only=self.opts.user_repos_only)
        else:
            self.rdb = repo_db.RepoDB()

    def _setup_logger(self):
        self.opts.loglevel = logging.INFO
        # TODO: log to file if specified, with requested severity
        self.logger = logging.getLogger()
        self.logger.setLevel(self.opts.loglevel)
        ch = logging.StreamHandler(sys.stdout)
        ch.setLevel(self.opts.loglevel)
        ch.setFormatter(logging.Formatter("%(message)s"))
        self.logger.addHandler(ch)
        # if self.opts.logfile:
        #     self.logger.addHandler(logfilehandler)

    def required_repos(self):
        """Return a list of RepoTuples that match the specified role,
        subscription type and oo-version

        """
        # Include the base RHEL repo in the required repos
        roles = self.opts.role + ['base']
        sub = self.opts.subscription
        o_ver = self.opts.oo_version
        return flatten_uniq([self.rdb.find_repos(subscription=sub,
                                            role=role,
                                            product_version=o_ver)
                        for role in roles])

    def required_repoids(self):
        """Return a list of repoids as Strings that match the specified role,
        subscription type and oo-version

        """
        return [repo.repoid for repo in self.required_repos()]

    def enabled_blessed_repos(self):
        """Return a list of RepoTuples from self.rdb that match repoids of
        enabled repositories

        """
        enabled = self.oscs.enabled_repoids()
        return [repo for repo in self.rdb.find_repos_by_repoid(enabled)
                if repo.subscription == self.opts.subscription
                and repo.product_version == self.opts.oo_version]

    def blessed_repoids(self, **kwargs):
        """Return a list of just repoids for the results of blessed_repos
        called with the provided arguments

        """
        return [repo.repoid for repo in self.blessed_repos(**kwargs)]

    def blessed_repos(self, enabled = False, required = False, product = None):
        """Return a list of RepoTuples from self.rdb that match the provided
        criteria

        Keyword arguments:
        enabled -- if True, constrain results to those matching the
                   repoids of currently enabled repositories
                   Default: False
        required -- if True, constrain search to the list provided by
                    required_repos
                    Default: False
        product -- if provided, constrain results to RepoTuples with a
                   product field that matches the string provided
                   Default: None
        """

        kwargs = {'subscription': self.opts.subscription,
                  'product_version': self.opts.oo_version}
        if product:
            kwargs['product'] = product
        if enabled:
            if required:
                return [repo for repo in self.required_repos()
                        if repo.repoid in self.oscs.enabled_repoids()
                        and (not product or repo.product == product)]
            return [repo for repo in self.rdb.find_repos(**kwargs)
                    if repo.repoid in self.oscs.enabled_repoids()]
        if required:
            return [repo for repo in self.required_repos()
                    if not product or repo.product == product]
        return self.rdb.find_repos(**kwargs)

    def _sub(self, subscription):
        self.opts.subscription = subscription
        self.logger.info('Detected OpenShift Enterprise repository '
                         'subscription managed by %s.' %
                         SUBS_NAME[self.opts.subscription])

    def _oo_ver(self, version):
        self.opts.oo_version = version
        self.logger.info('Detected installed OpenShift Enterprise '
                         'version %s' % self.opts.oo_version)

    def _sub_ver(self, subscription, version = None):
        if self.opts.subscription == UNKNOWN and not self.opts.oo_version:
            self._sub(subscription)
            if version:
                self._oo_ver(version)
                return True
            # We still haven't gotten a version guess - fail to force
            # user to specify version
            return False
        if self.opts.subscription == UNKNOWN and self.opts.oo_version:
            if not version or version == self.opts.oo_version:
                self._sub(subscription)
                return True
        if self.opts.subscription != UNKNOWN and not self.opts.oo_version:
            if subscription == self.opts.subscription and version:
                self._oo_ver(version)
                return True
        if self.opts.subscription != UNKNOWN and self.opts.oo_version:
            if (subscription == self.opts.subscription and
                (not version or version == self.opts.oo_version)):
                return True
        return False

    def guess_ose_version_and_subscription(self):
        """Attempt to determine the product version and subscription
        management tool in use if one or both arguments aren't
        provided by the user.

        TODO: Better description of guess criteria
        """
        if self.opts.subscription != UNKNOWN and self.opts.oo_version:
            # Short-circuit guess if user specifies sub and ver
            return True
        matches = self.rdb.find_repos_by_repoid(self.oscs.all_repoids())
        rhsm_ose_2_0 = [repo for repo in matches if
                        repo in self.rdb.find_repos(subscription = 'rhsm',
                                                  product_version = '2.0',
                                                  product = 'ose')]
        rhn_ose_2_0 = [repo for repo in matches if
                       repo in self.rdb.find_repos(subscription = 'rhn',
                                                 product_version = '2.0',
                                                 product = 'ose')]
        rhsm_ose_1_2 = [repo for repo in matches if
                        repo in self.rdb.find_repos(subscription = 'rhsm',
                                                  product_version = '1.2',
                                                  product = 'ose')]
        rhn_ose_1_2 = [repo for repo in matches if
                       repo in self.rdb.find_repos(subscription = 'rhn',
                                                 product_version = '1.2',
                                                 product = 'ose')]
        rhsm_2_0_avail = [repo for repo in rhsm_ose_2_0 if repo.repoid in
                          self.oscs.enabled_repoids()]
        rhn_2_0_avail = [repo for repo in rhn_ose_2_0 if repo.repoid in
                         self.oscs.enabled_repoids()]
        rhsm_1_2_avail = [repo for repo in rhsm_ose_1_2 if repo.repoid in
                          self.oscs.enabled_repoids()]
        rhn_1_2_avail = [repo for repo in rhn_ose_1_2 if repo.repoid in
                         self.oscs.enabled_repoids()]
        rhsm_2_0_pkgs = filter(None,
                               [self.oscs.verify_package(repo.key_pkg,
                                                         source=repo.repoid)
                                for repo in rhsm_2_0_avail])
        rhn_2_0_pkgs = filter(None,
                              [self.oscs.verify_package(repo.key_pkg,
                                                        source=repo.repoid)
                               for repo in rhn_2_0_avail])
        rhsm_1_2_pkgs = filter(None,
                               [self.oscs.verify_package(repo.key_pkg,
                                                         source=repo.repoid)
                                for repo in rhsm_1_2_avail])
        rhn_1_2_pkgs = filter(None,
                              [self.oscs.verify_package(repo.key_pkg,
                                                        source=repo.repoid)
                               for repo in rhn_1_2_avail])
        # This if ladder detects the subscription type and version
        # based on available OSE repos and which repos provide
        # installed packages. Maybe there's a better way?
        if ((rhsm_2_0_pkgs and self._sub_ver('rhsm', '2.0')) or
            (rhn_2_0_pkgs and self._sub_ver('rhn', '2.0')) or
            (rhsm_1_2_pkgs and self._sub_ver('rhsm', '1.2')) or
            (rhn_1_2_pkgs and self._sub_ver('rhn', '1.2')) or
            (rhsm_2_0_avail and self._sub_ver('rhsm', '2.0')) or
            (rhn_2_0_avail and self._sub_ver('rhn', '2.0')) or
            (rhsm_1_2_avail and self._sub_ver('rhsm', '1.2')) or
            (rhn_1_2_avail and self._sub_ver('rhn', '1.2')) ):
            return True
        # This section detects just the subscription type if the
        # version has been specified or couldn't be determined by the
        # preceding logic.
        for fxn_rcheck, sub in [(self.oscs.repo_is_rhsm, 'rhsm'),
                                (self.oscs.repo_is_rhn, 'rhn')]:
            if self.opts.subscription == UNKNOWN:
                for repoid in self.oscs.all_repoids():
                    if fxn_rcheck(repoid) and self._sub_ver(sub):
                        return True
            else:
                # No need to check for a value the user has provided
                break
        return False

    def check_version_conflict(self):
        """Determine if repositories for multiple versions of OpenShift have
        been wrongly enabled, and advise or fix accordingly.
        """
        matches = self.rdb.find_repos_by_repoid(self.oscs.enabled_repoids())
        conflicts = filter(lambda repo:
                           not hasattr(repo.product_version, '__iter__') and
                           not (repo.product_version == self.opts.oo_version),
                           matches)
        if conflicts:
            self.problem = True
            if self.opts.fix:
                for repo in conflicts:
                    if self.oscs.disable_repo(repo.repoid):
                        self.logger.warning('Disabled repository %s' %
                                            repo.repoid)
            else:
                rhsm_conflicts = [repo.repoid for repo in conflicts if
                                  self.oscs.repo_is_rhsm(repo.repoid)]
                rhn_conflicts = [repo.repoid for repo in conflicts if
                                 self.oscs.repo_is_rhn(repo.repoid)]
                other_conflicts = [repo.repoid for repo in conflicts if
                                   not (repo.repoid in rhsm_conflicts or
                                        repo.repoid in rhn_conflicts)]
                if rhsm_conflicts:
                    self.logger.error('The following OpenShift Enterprise '
                                      'repositories conflict with the '
                                      'detected or specified product version.')
                    self.logger.error('To prevent package conflicts, disable '
                                      'these repositories by running these '
                                      'commands:')
                    for repoid in rhsm_conflicts:
                        self.logger.error('    # subscription-manager repos '
                                          '--disable=%s' % repoid)
                if rhn_conflicts:
                    self.logger.error('The following RHN Classic or RHN '
                                      'Satellite-managed OpenShift Enterprise '
                                      'repositories conflict with the '
                                      'detected or specified product version.')
                    self.logger.error('To prevent package conflicts, disable '
                                      'these repositories by making the '
                                      'following modifications to '
                                      '/etc/yum/pluginconf.d/rhnplugin.conf')
                    for repoid in rhn_conflicts:
                        self.logger.error('    Set enabled=0 in the [%s] '
                                          'section' % repoid)
                if other_conflicts:
                    self.logger.error('The following Yum repositories conflict '
                                      'with the detected or specified product '
                                      'version.')
                    self.logger.error('Disable these repositories by running '
                                      'these commands:')
                    for repoid in other_conflicts:
                        self.logger.error('    # yum-config-manager '
                                          '--disable %s' % repoid)
            return False
        return True


    def verify_yum_plugin_priorities(self):
        """Determine if the required yum plugin package yum-plugin-priorities
        is installed. No action should be taken if the package can't
        be found (advise only)

        """
        self.logger.info('Checking if yum-plugin-priorities is installed')
        try:
            if not self.oscs.verify_package('yum-plugin-priorities'):
                self.problem = True
                if self.oscs.package_available('yum-plugin-priorities'):
                    self.logger.error('Required package yum-plugin-priorities '
                                      'is not installed. Install the package '
                                      'with the following command:')
                    self.logger.error('# yum install yum-plugin-priorities')
                else:
                    self.logger.error('Required package yum-plugin-priorities '
                                      'is not available.')
                return False
        except Errors.RepoError as exc:
            raise UnrecoverableYumError(exc)
        return True

    def _get_pri(self, repoid):
        return self.resolved_repos.get(repoid, self.oscs.repo_priority(repoid))

    def _limit_pri(self, repolist, minpri=False):
        """Determine the highest or lowest priority for the provided repos,
        depending on minpri value
        """
        res = -1
        c_fxn, p_limit = max, 0
        if minpri:
            c_fxn, p_limit = min, 99
        res = c_fxn(chain((self._get_pri(repoid) for
                           repoid in repolist), [p_limit]))
        return res

    def _set_pri(self, repoid, priority):
        self.problem = True
        if not self.pri_header:
            self.pri_header = True
            self.logger.info('Resolving repository/channel/subscription '
                             'priority conflicts')
        if self.opts.fix:
            self.logger.warning('Setting priority for repository %s to %d' %
                                (repoid, priority))
            self.oscs.set_repo_priority(repoid, priority)
        else:
            if not self.pri_resolve_header:
                self.pri_resolve_header = True
                if self.oscs.repo_is_rhn(repoid):
                    self.logger.error('To resolve conflicting repositories, '
                                      'update '
                                      '/etc/yum/pluginconf.d/rhnplugin.conf '
                                      'with the following changes:')
                else:
                    self.logger.error('To resolve conflicting repositories, '
                                      'update repo priority by running:')
            # TODO: in the next version this should read
            #'# subscription-manager override --repo=%s --add=priority:%d'
            if self.oscs.repo_is_rhn(repoid):
                self.logger.error('    Set priority=%d in the [%s] section' %
                                  (priority, repoid))
            else:
                self.logger.error('# yum-config-manager '
                                  '--setopt=%s.priority=%d %s --save' %
                                  (repoid, priority, repoid))

    def _commit_resolved_pris(self):
        for repoid, pri in sorted(self.resolved_repos.items(),
                                  key = lambda (kk, vv): vv):
            if not self.committed_resolved_repos.get(repoid, None) == pri:
                self._set_pri(repoid, pri)
        self.committed_resolved_repos = self.resolved_repos.copy()


    def _check_valid_pri(self, repos):
        bad_repos = [(repoid, self._get_pri(repoid)) for
                     repoid in repos if self._get_pri(repoid) >= 99]
        if bad_repos:
            self.problem = True
            self.logger.error('The calculated priorities for the following '
                              'repoids are too large (>= 99)')
            for repoid, pri in bad_repos:
                self.logger.error('    %s'%repoid)
            self.logger.error('Please re-run this script with the --fix '
                              'argument to set an appropriate priority, '
                              'or update the system priorities by hand')
            return False
        return True

    def verify_rhel_priorities(self, ose_repos, rhel6_repos):
        """Check that the base Red Hat Enterprise Linux repositories are lower
        priority than the OpenShift repositories and fix or advise
        accordingly

        """
        res = True
        ose_pri = self._limit_pri(ose_repos)
        # rhel_pri = self._get_pri(rhel6_repo)
        rhel_pri = self._limit_pri(rhel6_repos, minpri=True)
        if rhel_pri <= ose_pri:
            for repoid in ose_repos:
                self.resolved_repos[repoid] = OSE_PRIORITY
                res = False
            ose_pri = OSE_PRIORITY
        if rhel_pri <= ose_pri or rhel_pri >= 99:
            for repoid in rhel6_repos:
                self.resolved_repos[repoid] = RHEL_PRIORITY
            res = False
        return res

    def verify_jboss_priorities(self, ose_repos, jboss_repos, rhel6_repos=None):
        """Check that the JBoss EAP and EWS repositories are lower priority
        than the base Red Hat Enterprise Linux repositories and fix or
        advise accordingly

        """
        res = True
        min_pri = self._limit_pri(ose_repos)
        jboss_pri = self._limit_pri(jboss_repos, minpri=True)
        jboss_max_pri = self._limit_pri(jboss_repos)
        if rhel6_repos:
            min_pri = self._limit_pri(rhel6_repos)
        if jboss_pri <= min_pri or jboss_max_pri >= 99:
            if rhel6_repos:
                for repoid in rhel6_repos:
                    self.resolved_repos[repoid] = RHEL_PRIORITY
            res = False
            for repoid in jboss_repos:
                self.resolved_repos[repoid] = JBOSS_PRIORITY
                res = False
        return res

    def verify_priorities(self):
        """Verify that the relative priorities of the blessed repositories are
        correctly ordered and fix or advise accordingly

        """
        res = True
        self.logger.info('Checking channel/repository priorities')
        ose_scl = self.blessed_repoids(enabled=True, required=True,
                                       product='ose')
        ose_scl += self.blessed_repoids(enabled=True, required=True,
                                        product='rhscl')
        jboss = self.blessed_repoids(enabled=True, required=True,
                                     product='jboss')
        rhel = self.blessed_repoids(enabled=True, product='rhel')
        if rhel:
            res &= self.verify_rhel_priorities(ose_scl, rhel)
        if jboss:
            if rhel:
                res &= self.verify_jboss_priorities(ose_scl, jboss, rhel)
            else:
                res &= self.verify_jboss_priorities(ose_scl, jboss)
        self._commit_resolved_pris()
        return res

    def check_disabled_repos(self):
        """Check if any required repositories are disabled, and fix or advise
        accordingly
        """
        disabled_repos = list(set(self.blessed_repoids(required = True))
                              .intersection(self.oscs.disabled_repoids()))
        if disabled_repos:
            self.problem = True
            if self.opts.fix:
                for repo in disabled_repos:
                    if self.oscs.enable_repo(repo):
                        self.logger.warning('Enabled repository %s'%repo)
            else:
                self.logger.error('The required OpenShift Enterprise '
                                  'repositories are disabled:')
                for repo in disabled_repos:
                    self.logger.error('    %s'%repo)
                if self.opts.subscription == RHN:
                    self.logger.error('Make the following modifications '
                                      'to /etc/yum/pluginconf.d/rhnplugin.conf')
                else:
                    self.logger.error('Enable these repositories by running '
                                      'these commands:')
                for repoid in disabled_repos:
                    if self.opts.subscription == RHN:
                        self.logger.error('    Set enabled=1 in the [%s] '
                                          'section' % repoid)
                    elif self.opts.subscription == RHSM:
                        self.logger.error('# subscription-manager repos '
                                          '--enable=%s' % repoid)
                    else:
                        self.logger.error('# yum-config-manager --enable %s' %
                                          repoid)
            return False
        return True

    def check_missing_repos(self):
        """Check if any required repositories are missing, and advise
        accordingly

        """
        missing_repos = [repo for repo in
                         self.blessed_repoids(required = True)
                         if repo not in self.oscs.all_repoids()]
        if missing_repos:
            self.problem = True
            self.logger.error('The required OpenShift Enterprise repositories '
                              'are missing:')
            for repo in missing_repos:
                self.logger.error('    %s'%repo)
            self.logger.error('Please verify that an OpenShift Enterprise '
                              'subscription is attached to this system using '
                              'either RHN Classic or Red Hat Subscription '
                              'Manager by following the instructions here: %s' %
                              ATTACH_ENTITLEMENTS_URL)
            return False
        return True

    def verify_repo_priority(self, repoid, required_repos):
        """Checks the given repoid to make sure that the priority for it
        doesn't conflict with required repository priorities

        Preconditions: Maximum OpenShift (and blessed) repository
        priority should be below 99
        """
        res = True
        required_pri = self._limit_pri(required_repos)
        new_pri = OTHER_PRIORITY
        if self._get_pri(repoid) <= required_pri:
            if required_pri >= new_pri:
                new_pri = min(99, required_pri+10)
            # self._set_pri(repoid, new_pri)
            self.resolved_repos[repoid] = new_pri
            res = False
        return res


    def find_package_conflicts(self):
        """Search for packages from non-blessed repositories which could
        conflict with the "official" packages provided in the blessed
        repositories, determine an appropriate priority for the
        non-blessed repos to resolve the conflict, and fix or advise
        accordingly

        """
        res = True
        self.pri_resolve_header = False
        all_blessed_repos = self.rdb.find_repoids(
            product_version=self.opts.oo_version)
        enabled_ose_scl_repos = self.blessed_repoids(enabled=True,
                                                     required=True,
                                                     product='ose')
        enabled_ose_scl_repos += self.blessed_repoids(enabled=True,
                                                      required=True,
                                                      product='rhscl')
        enabled_jboss_repos = self.blessed_repoids(enabled=True,
                                                   required=True,
                                                   product='jboss')
        rhel6_repos = self.blessed_repoids(enabled=True, product='rhel')
        # if not rhel6_repo[0] in self.oscs.enabled_repoids():
        #     rhel6_repo = []
        required_repos = (enabled_ose_scl_repos + rhel6_repos +
                          enabled_jboss_repos)
        if not self._check_valid_pri(required_repos):
            return False
        for repoid in required_repos:
            try:
                ose_pkgs = self.oscs.packages_for_repo(repoid,
                                                       disable_priorities=True)
                ose_pkg_names = sorted(set([xx.name for xx in ose_pkgs]))
                matches = [xx for xx in
                           self.oscs.all_packages_matching(ose_pkg_names, True)
                           if xx.repoid not in all_blessed_repos]
                conflicts = sorted(set([xx.repoid for xx in matches]))
                for repo in conflicts:
                    res &= self.verify_repo_priority(repo, required_repos)
            except KeyError:
                self.logger.error('Repository %s not enabled'%repoid)
                res = False
            except Errors.RepoError as repo_err:
                raise UnrecoverableYumError(repo_err)
        # for repoid, pri in self.resolved_repos.iteritems():
        #     if not old_resolved_repos.get(repoid, None) == pri:
        #         self._set_pri(repoid, pri)
        self._commit_resolved_pris()
        return res

    def _set_exclude(self, repo):
        if self.opts.fix:
            self.logger.error('    %s: %s' %
                              (repo.repoid, ' '.join(repo.exclude)))
            self.oscs.merge_excludes(repo.repoid, list(repo.exclude))
        else:
            exc = ' '.join(repo.exclude)
            if self.opts.subscription == RHN or self.opts.subscription == RHSM:
                self.logger.error('Add the following to the [%s] section:' %
                                  (repo.repoid))
                self.logger.error('    exclude=%s'%exc)
            else:
                repofile = self.oscs.yum_base.repos.repos[repo.repoid].repofile
                if repofile:
                    self.logger.error('In file %s, Add the following to the '
                                      '[%s] section' % (repofile, repo.repoid))
                    self.logger.error('    exclude=%s'%exc)
                else: # Man, I don't know
                    self.logger.error('# yum-config-manager '
                                      '--setopt=%s.exclude=%d %s --save' %
                                      (repo.repoid, exc, repo.repoid))

    def _excludes_needed(self, repo):
        if repo.exclude and repo.repoid in self.oscs.all_repoids():
            cur_excl = self.oscs.repo_for_repoid(repo.repoid).exclude
            # see if current set exclusions are (improper) superset of repo.exclude
            return not (set(cur_excl) >= set(repo.exclude))

    def set_excludes(self):
        """Set any excludes configured for the required repositories
        """
        need_exclude = [repo for repo in self.required_repos() if
                        self._excludes_needed(repo)]
        if need_exclude:
            self.problem = True
            if self.opts.fix:
                self.logger.error('Setting package exclusions for the '
                                  'following repositories:')
            else:
                self.logger.error('The following repositories need package '
                                  'exclusions set:')
                for repo in need_exclude:
                    self.logger.error('    %s'%repo.repoid)
                if self.opts.subscription == RHN:
                    self.logger.error('Make the following modifications to '
                                      '/etc/yum/pluginconf.d/rhnplugin.conf')
                elif self.opts.subscription == RHSM:
                    self.logger.error('Make the following modifications to '
                                      '/etc/yum.repos.d/redhat.repo')
                else:
                    self.logger.error('Modify the repositories by running '
                                      'these commands:')
            for repo in need_exclude:
                self._set_exclude(repo)
            return False
        return True

    def guess_role(self):
        """Try to determine the system role by comparing installed packages to
        key packages

        """
        self.logger.warning('No roles have been specified. Attempting to '
                            'guess the roles for this system...')
        self.opts.role = []
        for role in VALID_ROLES:
            # get uniquified list of packages that coorespond to only one role
            check_pkgs = list(set([repo.key_pkg for repo in
                                   self.rdb.find_repos(role=role) if
                                   not hasattr(repo.role, '__iter__')]))
            if filter(self.oscs.verify_package, check_pkgs):
                self.opts.role.append(role)
        if not self.opts.role:
            self.logger.error('No roles could be detected.')
            self.problem = True
            return False
        self.logger.warning('If the roles listed below are incorrect or '
                            'incomplete, please re-run this script with the '
                            'appropriate --role arguments')
        self.logger.warning('\n'.join(('    %s' %
                                       role for role in self.opts.role)))
        return True

    def validate_roles(self):
        """Check supplied roles against VALID_ROLES.

        TODO: This probably isn't necessary any longer
        """
        if not self.opts.role:
            return True
        for role in self.opts.role:
            if not role in VALID_ROLES:
                self.problem = True
                self.logger.error('You have specified an invalid role: %s '
                                  'is not one of %s' % (role, VALID_ROLES))
                self.opt_parser.print_help()
                return False
        return True

    def validate_version(self):
        """Check supplied product version against VALID_OO_VERSIONS.

        TODO: This probably isn't necessary any longer

        """
        if self.opts.oo_version:
            if not self.opts.oo_version in VALID_OO_VERSIONS:
                self.logger.error('You have specified an invalid version: '
                                  '%s is not one of %s' %
                                  (self.opts.oo_version, VALID_OO_VERSIONS))
                self.opt_parser.print_help()
                return False
        return True

    def massage_roles(self):
        """Set supplied roles to lowercase, guarantee that the "node" role is
        set if "node-eap" is set, and advise user on "node-eap" role
        if "node" is set without "node-eap"

        """
        if not self.opts.role:
            self.guess_role()
        if self.opts.role:
            self.opts.role = [xx.lower() for xx in self.opts.role]
            if 'node-eap' in self.opts.role and not 'node' in self.opts.role:
                self.opts.role.append('node')
            if 'node' in self.opts.role and not 'node-eap' in self.opts.role:
                # self.problem = True
                self.logger.warning('If this system will be providing the '
                                    'JBossEAP cartridge, re-run this '
                                    'command with the --role=node-eap argument')

    def run_checks(self):
        if not self.validate_roles():
            return False
        self.massage_roles()
        if not self.guess_ose_version_and_subscription():
            self.problem = True
            if self.opts.subscription == UNKNOWN:
                self.logger.error('Could not determine subscription type.')
                self.logger.error('Please attach an OpenShift Enterprise '
                                  'subscription to this system using either '
                                  'RHN Classic or Red Hat Subscription '
                                  'Manager by following the instructions '
                                  'here: %s' % ATTACH_ENTITLEMENTS_URL)
            if not self.opts.oo_version:
                self.logger.error('Could not determine product version. '
                                  'Please re-run this script with the '
                                  '--oo-version argument.')
            return False
        print ""
        if not (self.check_version_conflict() or self.opts.report_all):
            return False
        if not (self.check_disabled_repos() or self.opts.report_all):
            return False
        if not (self.check_missing_repos() or self.opts.report_all):
            return False
        if self.opts.role:
            if not (self.verify_yum_plugin_priorities() or
                    self.opts.report_all):
                self.logger.warning('Skipping yum priorities verification')
                return False
            if not (self.verify_priorities() or self.opts.report_all):
                return False
            if not (self.find_package_conflicts() or self.opts.report_all):
                return False
            if not (self.set_excludes() or self.opts.report_all):
                return False
        else:
            self.logger.warning('Please specify at least one role for this '
                                'system with the --role command')
            self.problem = True
            return False
        if not self.problem:
            self.logger.info('No problems could be detected!')
            return True
        return False

    def main(self):
        if self.opts.report_all:
            # report_all + fix == fix_all, we don't want someone to
            # accidentally set that up by hand
            self.opts.fix = False
        if self.opts.fix_all:
            self.opts.report_all = True
            self.opts.fix = True
        try:
            self.run_checks()
            if not self.opts.fix and self.problem:
                self.logger.info('Please re-run this tool after making '
                                 'any recommended repairs to this system')
            return not self.problem
        except UnrecoverableYumError, e:
            self.logger.critical('An unrecoverable error prevents further '
                                 'checks from being run. Re-run this tool '
                                 'after the problem has been repaired:')
            print ''
            self.logger.critical(e)
            return 255

if __name__ == "__main__":
    ROLE_HELP = 'OpenShift component role(s) this system will fulfill.'
    OO_VERSION_HELP = 'Version of OpenShift Enterprise in use on this system.'
    SUBSCRIPTION_HELP = ('Subscription management system which provides the '
                         'OpenShift Enterprise repositories/channels.')
    FIX_HELP = 'Attempt to repair the first problem found.'
    FIX_ALL_HELP = 'Attempt to repair all problems found.'
    REPORT_ALL_HELP = ('Report all problems (default is to halt after first '
                       'problem report.)')
    REPO_CONF_HELP = ('Load blessed repository data from the specified file '
                      'instead of built-in values')
    USER_REPOS_HELP = ('Requires --repo-config. Blend the repository data '
                       'loaded via --repo-config with the built-in values')

    # TODO: This is getting unwieldy - time for a wrapper?
    try:
        import argparse
        opt_parser = argparse.ArgumentParser()
        opt_parser.add_argument('-r', '--role', default=None,
                                choices=VALID_ROLES, action='append',
                                help=ROLE_HELP)
        opt_parser.add_argument('-o', '--oo_version', '--oo-version',
                                default=None,
                                choices=VALID_OO_VERSIONS,
                                dest='oo_version',
                                help=OO_VERSION_HELP)
        opt_parser.add_argument('-s', '--subscription-type',
                                default=None, choices=VALID_SUBS,
                                dest='subscription',
                                help=SUBSCRIPTION_HELP)
        opt_parser.add_argument('-f', '--fix', action='store_true',
                                default=False, help=FIX_HELP)
        opt_parser.add_argument('-a', '--fix-all',
                                action='store_true', default=False,
                                dest='fix_all', help=FIX_ALL_HELP)
        opt_parser.add_argument('-p', '--report-all',
                                action='store_true', default=False,
                                dest='report_all',
                                help=REPORT_ALL_HELP)
        opt_parser.add_argument('-c', '--repo-config', default=None,
                                type=str, help=REPO_CONF_HELP)
        # opt_parser.add_argument('-u', '--blend-user-repos',
        #                         action='store_true', default=True,
        #                         dest='user_repos_only',
        #                         help=USER_REPOS_HELP)
        opts = opt_parser.parse_args()
    except ImportError:
        import optparse
        ROLE_HELP += ' One or more of: %s' % VALID_ROLES
        OO_VERSION_HELP += ' One of: %s' % VALID_OO_VERSIONS
        SUBSCRIPTION_HELP += ' One of: %s' % VALID_SUBS
        opt_parser = optparse.OptionParser()
        opt_parser.add_option('-r', '--role', default=None,
                              choices=VALID_ROLES, action='append',
                              help=ROLE_HELP)
        opt_parser.add_option('-o', '--oo_version', '--oo-version',
                              default=None, choices=VALID_OO_VERSIONS,
                              dest='oo_version', help=OO_VERSION_HELP)
        opt_parser.add_option('-s', '--subscription-type',
                              default=None, choices=VALID_SUBS,
                              dest='subscription',
                              help=SUBSCRIPTION_HELP)
        opt_parser.add_option('-f', '--fix', action='store_true',
                              default=False, help=FIX_HELP)
        opt_parser.add_option('-a', '--fix-all', action='store_true',
                              default=False, dest='fix_all', help=FIX_ALL_HELP)
        opt_parser.add_option('-p', '--report-all',
                              action='store_true', default=False,
                              dest='report_all', help=REPORT_ALL_HELP)
        opt_parser.add_option('-c', '--repo-config', default=None,
                              type='string', help=REPO_CONF_HELP)
        # opt_parser.add_option('-u', '--blend-user-repos',
        #                       action='store_true', default=True,
        #                       dest='user_repos_only',
        #                       help=USER_REPOS_HELP)
        (opts, args) = opt_parser.parse_args()
    opts.user_repos_only = True
    oacs = OpenShiftAdminCheckSources(opts, opt_parser)
    if not oacs.main():
        sys.exit(1)
    sys.exit(0)
