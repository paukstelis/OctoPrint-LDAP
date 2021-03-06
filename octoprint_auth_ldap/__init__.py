# coding=utf-8
from __future__ import absolute_import

import json
import ldap
from ldap.filter import filter_format
from octoprint.settings import settings
from octoprint.plugin import SettingsPlugin, TemplatePlugin
from octoprint.access.users import FilebasedUserManager, User, UserManager, UserAlreadyExists, UnknownUser
from octoprint.access.groups import FilebasedGroupManager, Group, GroupManager, GroupNeed
from octoprint.util import atomic_write
import os
import yaml


class LDAPUser(User):
    USER_TYPE = "LDAP"

    # noinspection SpellCheckingInspection,PyShadowingNames
    def __init__(self, username, active=True, permissions=None, dn=None, groups=None, apikey=None, settings=None):
        User.__init__(self, username, None, active, permissions, apikey, settings)
        self._dn = dn
        self._groups = groups

    def get_distinguished_name(self):
        return self._dn

    def get_groups(self):
        return self._groups

    def set_groups(self, groups):
        self._groups = groups

#now needed to init FilebasedUserManager
class LDAPGroupManager(FilebasedGroupManager):
    def __init__(self, plugin, **kwargs):
        self._plugin = plugin
        FilebasedGroupManager.__init__(self, **kwargs)


class LDAPUserManager(FilebasedUserManager):

    def __init__(self, plugin, **kwargs):
        self._plugin = plugin
        # noinspection PyArgumentList
        ldapgroups = LDAPGroupManager(self)
        FilebasedUserManager.__init__(self, ldapgroups, **kwargs)

    def plugin_settings(self):
        return self._plugin.get_settings()

    def find_user(self, userid=None, apikey=None, session=None):
        self._logger.debug("Search for userid=%s, apiKey=%s, session=%s" % (userid, apikey, session))
        user = FilebasedUserManager.find_user(self, userid=userid, apikey=apikey, session=session)

        transformation = self.plugin_settings().get(["search_term_transform"])
        if not user and userid and transformation:
            self._logger.debug("Transforming %s using %s" % (userid, transformation))
            transformed = getattr(str, transformation)(str(userid))
            self._logger.debug("Search for user userid=%s" % transformed)
            if transformed != userid:
                userid = transformed
                user = FilebasedUserManager.find_user(self, userid=userid, apikey=apikey, session=session)

        if not user and userid:
            self._logger.debug("User %s not found locally, treating as LDAP" % userid)
            search_filter = self.plugin_settings().get(["search_filter"])

            """
            operating on the wildly unsafe assumption that the admin who configures this plugin will have their head
            screwed on right and NOT escaping their search strings... only escaping unsafe user-entered text that is
            passed directly to search filters
            """
            ldap_user = self.ldap_search(filter_format(search_filter, (userid,)))

            if ldap_user is not None:
                self._logger.debug("User %s found as dn=%s" % (userid, ldap_user["dn"]))
                groups = self.group_filter(ldap_user["dn"])
                if isinstance(groups, list):
                    self._logger.debug("Creating new LDAPUser %s" % userid)
                    # TODO: make username configurable or make dn configurable (e.g. could be userPrincipalName?)
                    if self.plugin_settings().get(["local_cache"]):
                        self.add_user(username=userid, dn=ldap_user["dn"], groups=groups, active=True)
                        user = self._users[userid]
                    else:
                        user = LDAPUser(username=userid, dn=ldap_user["dn"], groups=groups, active=True, )
        return user

    """
    this will return either an empty list (i.e. []) (if no groups are specified in configuration) or a list of groups
    that the user is currently a member of (e.g. ["Lab Users"]), or False if the groups filter is configured and the
    user is not a member of any configured groups
    """

    def group_filter(self, dn):
        groups = self.plugin_settings().get(["groups"])
        actual_groups = []
        if groups:
            group_filter = self.plugin_settings().get(["group_filter"])
            group_member_filter = self.plugin_settings().get(["group_member_filter"])
            for group in str(groups).split(","):
                result = self.ldap_search("(&" +
                                          "(" + group_filter % group.strip() + ")" +
                                          "(" + (group_member_filter % dn) + ")" +
                                          ")")
                self._logger.debug("Found %s" % json.dumps(result))
                if result is not None:
                    actual_groups.append(group)
            if not actual_groups:
                return False
        return actual_groups

    def default_roles(self):
        roles = []
        if self.plugin_settings().get(["default_role_user"]):
            roles.append("user")
        if self.plugin_settings().get(["default_role_admin"]):
            roles.append("admin")
        return roles

    def add_user(self, username, password, active=False, permissions=None, groups=None, apikey=None, overwrite=False, dn=None):
        if not permissions:
            permissions = []
        permissions = self._to_permissions(*permissions)

        if not groups:
            groups = self._group_manager.default_groups
        groups = self._to_groups(*groups)

        if username in self._users and not overwrite:
            raise UserAlreadyExists(username)

        if dn and not password:
            if not groups:
                groups = []
            self._users[username] = LDAPUser(username, active, permissions, dn, groups, apikey)
        else:
            self._users[username] = User(username,
                                     UserManager.create_password_hash(password,
                                                                      settings=self._settings),
                                     active,
                                     permissions,
                                     groups,
                                     apikey=apikey)
        self._dirty = True
        self._save()

    def check_password(self, username, password):
        user = self.find_user(userid=username)
        self._logger.debug("%s is a %s" % (username, type(user)))
        if isinstance(user, LDAPUser):
            # in case group settings changed either in auth_ldap settings OR on LDAP directory
            groups = self.group_filter(user.get_distinguished_name())
            if user.is_active() and isinstance(groups, list):
                self.changeUserGroups(user.get_name(), groups)
                self._logger.debug("Checking %s password via LDAP" % user.get_name())
                client = self.get_ldap_client(user.get_distinguished_name(), password)
                return client is not None
            else:
                self._logger.debug("%s is inactive or no longer a member of required groups" % user.get_name())
        else:
            self._logger.debug("Checking %s password via users.yaml" % user.get_name())
            return FilebasedUserManager.check_password(self, user.get_name(), password)
        return False

    # noinspection PyPep8Naming
    def changeUserGroups(self, username, groups):
        if username not in self._users.keys():
            raise UnknownUser(username)

        if isinstance(self._users[username], LDAPUser) and self._users[username].get_groups() != groups:
            self._users[username].set_groups(groups)
            self._dirty = True
            self._save()

    def _save(self, force=False):
        if not self._dirty and not force:
            return

        data = {}
        for name in self._users.keys():
            user = self._users[name]
            if isinstance(user, LDAPUser):
                # noinspection PyProtectedMember
                data[name] = {
                    "type": LDAPUser.USER_TYPE,
                    "password": None,  # password field has to exist because of how FilebasedUserManager processes
                    # data, but an empty password hash cannot match any entered password (as
                    # whatever the user enters will be hashed... even an empty password.
                    "dn": user.get_distinguished_name(),
                    "groups": user.get_groups(),
                    "active": user.is_active(),
                    "roles": user.roles,
                    "apikey": user._apikey,
                    "settings": user.get_all_settings()
                }
            else:
                data[name] = {
                    "password": user._passwordHash,
                    "active": user._active,
                    "groups": self._from_groups(*user._groups),
                    "permissions": self._from_permissions(*user._permissions),
                    "apikey": user._apikey,
                    "settings": user._settings,

                    # TODO: deprecated, remove in 1.5.0
                    "roles": user._roles
                }

        with atomic_write(self._userfile, mode='wt', permissions=0o600, max_permissions=0o666) as f:
            yaml.safe_dump(data, f, default_flow_style=False, indent=4, allow_unicode=True)
            self._dirty = False
        self._load()

    """
    -------------------------------------------------------------------
    LDAP interactions
    -------------------------------------------------------------------
    """

    def get_ldap_client(self, user=None, password=None):
        uri = self.plugin_settings().get(["uri"])
        if not uri:
            self._logger.debug("No LDAP URI")
            return None

        if not user:
            user = self.plugin_settings().get(["auth_user"])
            password = self.plugin_settings().get(["auth_password"])

        try:
            self._logger.debug("Initializing LDAP connection to %s" % uri)
            client = ldap.initialize(uri)
            if self.plugin_settings().get(["request_tls_cert"]):
                self._logger.debug("Requesting TLS certificate")
                client.set_option(ldap.OPT_X_TLS_REQUIRE_CERT, ldap.OPT_X_TLS_DEMAND)
            else:
                client.set_option(ldap.OPT_X_TLS_REQUIRE_CERT, ldap.OPT_X_TLS_NEVER)
            if user is not None:
                self._logger.debug("Binding to LDAP as %s" % user)
                client.bind_s(user, password)
            return client
        except ldap.INVALID_CREDENTIALS:
            self._logger.error("Invalid credentials to bind to LDAP as %s" % user)
        except ldap.LDAPError as e:
            self._logger.error(json.dumps(e.message))
        return None

    def ldap_search(self, ldap_filter, base=None, scope=ldap.SCOPE_SUBTREE):
        if not base:
            base = self.plugin_settings().get(["search_base"])
        try:
            client = self.get_ldap_client()
            if client is not None:
                self._logger.debug("Searching LDAP, base: %s and filter: %s" % (base, ldap_filter))
                result = client.search_s(base, scope, ldap_filter)
                client.unbind_s()
                if result:
                    dn, data = result[0]
                    """
                    # Dump LDAP search query results to logger
                    self._logger.debug("dn: %s" % dn)
                    for key, value in data.iteritems():
                        self._logger.debug("%s: %s" % (key, value))
                    """
                    return dict(dn=dn, data=data)
        except ldap.LDAPError as e:
            self._logger.error(json.dumps(e.message))
        return None


class AuthLDAPPlugin(SettingsPlugin, TemplatePlugin):

    # noinspection PyUnusedLocal,PyShadowingNames
    def ldap_user_factory(self, components, settings):
        return LDAPUserManager(plugin=self)

    def get_settings(self):
        return self._settings

    # Softwareupdate hook

    def get_update_information(self):
        return dict(
            auth_ldap=dict(
                displayName=self._plugin_name,
                displayVersion=self._plugin_version,

                # version check: github repository
                type="github_release",
                user="gillg",
                repo="OctoPrint-LDAP",
                current=self._plugin_version,

                # update method: pip
                pip="https://github.com/gillg/OctoPrint-LDAP/archive/{target_version}.zip"
            )
        )

    # SettingsPlugin

    def get_settings_defaults(self):
        return dict(
            auth_password=None,
            auth_user=None,
            default_role_admin=False,
            default_role_user=True,
            group_filter="ou=%s",
            group_member_filter="uniqueMember=%s",
            groups=None,
            local_cache=False,
            request_tls_cert=None,
            search_base=None,
            search_filter="uid=%s",
            search_term_transform=None,
            uri=None
        )

    def get_settings_restricted_paths(self):
        return dict(
            admin=[
                ["auth_password"],
                ["auth_user"],
                ["default_role_admin"],
                ["default_role_user"],
                ["group_filter"],
                ["group_member_filter"],
                ["groups"],
                ["local_cache"],
                ["request_tls_cert"],
                ["search_base"],
                ["search_filter"],
                ["search_term_transform"],
                ["uri"],
            ],
            user=[],
            never=[]
        )

    def get_settings_version(self):
        return 2

    def on_settings_migrate(self, target, current):
        if current is None:
            self._logger.info(
                "Migrating %s settings from version %s to version %s" % (self._plugin_name, current, target))

            # migrate old settings to new locations and erase old settings
            prev_settings = dict(  # prev_setting_name="new_setting_name"
                ldap_uri="uri",
                ldap_tls_reqcert="request_tls_cert",
                ldap_search_base="search_base",
                ldap_groups="groups"
            )
            # noinspection PyTypeChecker
            for prev_key, key in prev_settings.iteritems():
                prev_value = settings().get(["accessControl", prev_key])
                if prev_value is not None:
                    cleaned_prev_value = prev_value
                    if prev_key == "ldap_tls_reqcert" and prev_value == "demand":
                        cleaned_prev_value = True
                    self._settings.set([key], cleaned_prev_value)
                    self._logger.info(
                        "accessControl.%s=%s setting migrated to plugins.auth_ldap.%s=%s"
                        % (prev_key, prev_value, key, cleaned_prev_value))
                settings().set(["accessControl", prev_key], None)

    # TemplatePlugin

    def get_template_configs(self):
        return [dict(type="settings", custom_bindings=False)]


__plugin_name__ = "Auth LDAP"


# noinspection PyGlobalUndefined
def __plugin_load__():
    global __plugin_implementation__
    __plugin_implementation__ = AuthLDAPPlugin()

    global __plugin_hooks__
    __plugin_hooks__ = {
        "octoprint.access.users.factory": __plugin_implementation__.ldap_user_factory,
        "octoprint.plugin.softwareupdate.check_config": __plugin_implementation__.get_update_information
    }

