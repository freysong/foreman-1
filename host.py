#!/usr/bin/env python
from __future__ import print_function
import copy
import os
import re
import requests
from requests.auth import HTTPBasicAuth
import sys
from time import time

import MySQLdb

try:
    import ConfigParser
except ImportError:
    import configparser as ConfigParser


try:
    import json
except ImportError:
    import simplejson as json


class ForemanInventory(object):
    config_paths = [
        "/Users/zhoukanggen/Desktop/python/foreman.ini",
        os.path.dirname(os.path.realpath(__file__)) + '/foreman.ini',
    ]

    def __init__(self):
        self.inventory = dict()  # A list of groups and the hosts in that group
        self.cache = dict()   # Details about hosts in the inventory
        self.facts = dict()   # Facts of each host
        self.hostlist = dict()
        # self.hostgroups = dict()  # host groups

    def run(self):
        # conn = MySQLdb.connect(host='10.61.2.225', port=3306, user='jumpserver',passwd='5Lov@wife', db='jumpserver', charset='utf8')
        self.read_settings()
        self._get_inventory()
        return True

    def _get_inventory(self):
        self.update_cache()

    def read_settings(self):
        """Reads the settings from the foreman.ini file"""

        config = ConfigParser.SafeConfigParser()
        env_value = os.environ.get('FOREMAN_INI_PATH')
        if env_value is not None:
            self.config_paths.append(
                os.path.expanduser(os.path.expandvars(env_value)))

        config.read(self.config_paths)

        # Foreman API related
        try:
            self.foreman_url = config.get('foreman', 'url')
            self.foreman_user = config.get('foreman', 'user')
            self.foreman_pw = config.get('foreman', 'password')
            self.foreman_ssl_verify = config.getboolean(
                'foreman', 'ssl_verify')
        except (ConfigParser.NoOptionError, ConfigParser.NoSectionError) as e:
            print("Error parsing configuration: %s" % e, file=sys.stderr)
            return False

    def _get_json(self, url, ignore_errors=None):
        page = 1
        results = []
        while True:
            ret = requests.get(url,
                               auth=HTTPBasicAuth(
                                   self.foreman_user, self.foreman_pw),
                               verify=self.foreman_ssl_verify,
                               params={'page': page, 'per_page': 250})
            if ignore_errors and ret.status_code in ignore_errors:
                break
            ret.raise_for_status()
            json = ret.json()
            # /hosts/:id has not results key
            if 'results' not in json:
                return json
            # Facts are returned as dict in results not list
            if isinstance(json['results'], dict):
                return json['results']
            # List of all hosts is returned paginaged
            results = results + json['results']
            if len(results) >= json['total']:
                break
            page += 1
            if len(json['results']) == 0:
                print("Did not make any progress during loop. "
                      "expected %d got %d" % (json['total'], len(results)),
                      file=sys.stderr)
                break
        return results

    def _get_hosts(self):
        return self._get_json("%s/api/v2/hosts" % self.foreman_url)

    def _get_hostgroup_by_id(self, hid):
        if hid not in self.hostgroups:
            url = "%s/api/v2/hostgroups/%s" % (self.foreman_url, hid)
            self.hostgroups[hid] = self._get_json(url)
        return self.hostgroups[hid]

    def _get_all_params_by_id(self, hid):
        url = "%s/api/v2/hosts/%s" % (self.foreman_url, hid)
        ret = self._get_json(url, [404])
        if ret == []:
            ret = {}
        return ret.get('all_parameters', {})

    def _get_facts_by_id(self, hid):
        url = "%s/api/v2/hosts/%s/facts" % (self.foreman_url, hid)
        return self._get_json(url)

    def _get_facts(self, host):
        """Fetch all host facts of the host"""
        ret = self._get_facts_by_id(host['id'])
        if len(ret.values()) == 0:
            facts = {}
        elif len(ret.values()) == 1:
            facts = list(ret.values())[0]
        else:
            raise ValueError(
                "More than one set of facts returned for '%s'" % host)
        print('*************************facts**************************************')
        hostlist = {}
        temp = list()
        try:
            for i in facts['interfaces'].split(','):
                if i == 'lo':
                    continue
                if i == 'docker0':
                    continue
                j = 'ipaddress_' + i
                for k in facts.keys():
                    if j == k:
                        temp.append(str(facts[k]))
                hostlist[facts['fqdn']] = temp
        except KeyError as e:
            print("The '%s'  facts is  not interfaces Key" % host)
            print(e)
            return facts

        conn = MySQLdb.connect(host='10.61.2.225', port=3306, user='jumpserver',
                               passwd='5Lov@wife', db='jumpserver', charset='utf8')
        if hostlist[facts['fqdn']][1:]:
            sql = [hostlist[facts['fqdn']][0], hostlist[facts['fqdn']][1],facts['fqdn'], 9999]
            insert = 'INSERT INTO `jasset_asset`(`ip`,`other_ip`,`hostname`,`port`) VALUES(%s)' % (','.join(["'%s'" % s for s in sql]))
        else:
            sql = [hostlist[facts['fqdn']][0], facts['fqdn'], 9999]
            insert = 'INSERT INTO `jasset_asset`(`ip`,`hostname`,`port`) VALUES(%s)' % (','.join(["'%s'" % s for s in sql]))
        print(insert)
        cur = conn.cursor()
        try:
            cur.execute(insert)
            cur.execute("commit")
            cur.close()
        except MySQLdb.Error as e:
            print(e)
        finally:
            conn.close()


        print('*************************facts**************************************')
        return facts

    def update_cache(self):
        """Make calls to foreman and save the output in a cache"""

        self.groups = dict()
        self.hosts = dict()

        for host in self._get_hosts():
            dns_name = host['name']
            # Create ansible groups for hostgroup
            group = 'hostgroup'
            val = host.get('%s_title' % group) or host.get('%s_name' % group)
            if val:
                safe_key = self.to_safe('%s_%s' % (
                    group, val.lower()))
                self.push(self.inventory, safe_key, dns_name)

            # Create ansible groups for environment, location and organization
            for group in ['environment', 'location', 'organization']:
                val = host.get('%s_name' % group)
                if val:
                    safe_key = self.to_safe('%s_%s' % (
                        group, val.lower()))
                    self.push(self.inventory, safe_key, dns_name)

            for group in ['lifecycle_environment', 'content_view']:
                val = host.get('content_facet_attributes',
                               {}).get('%s_name' % group)
                if val:
                    safe_key = self.to_safe('%s_%s' % (
                        group, val.lower()))
                    self.push(self.inventory, safe_key, dns_name)

            self.cache[dns_name] = host
            self.facts[dns_name] = self._get_facts(host)
            self.push(self.inventory, 'all', dns_name)

    def push(self, d, k, v):
        if k in d:
            d[k].append(v)
        else:
            d[k] = [v]

    @staticmethod
    def to_safe(word):
        regex = "[^A-Za-z0-9\_]"
        return re.sub(regex, "_", word.replace(" ", ""))

if __name__ == '__main__':
    inv = ForemanInventory()
    sys.exit(not inv.run())
