#!/usr/bin/python

import os
import urllib2
try:
    import FoundationPlist as plistlib
except ImportError:
    import plistlib

from distutils import version

MUNKI_URL = 'http://repo.sacredsf.org/munki'
MANIFESTS_URL = MUNKI_URL + '/manifests'
CATALOG_URL = MUNKI_URL + '/catalogs'
PKGS_URL = MUNKI_URL + '/pkgs'

# Stolen from munkicommon.py
class MunkiLooseVersion(version.LooseVersion):
    '''Subclass version.LooseVersion to compare things like
    "10.6" and "10.6.0" as equal'''
    
    def __init__(self, vstring=None):
        if vstring is None:
            # treat None like an empty string
            self.parse('')
        if vstring is not None:
            if isinstance(vstring, unicode):
                # unicode string! Why? Oh well...
                # convert to string so version.LooseVersion doesn't choke
                vstring = vstring.encode('UTF-8')
            self.parse(str(vstring))
    
    def _pad(self, version_list, max_length):
        """Pad a version list by adding extra 0
        components to the end if needed"""
        # copy the version_list so we don't modify it
        cmp_list = list(version_list)
        while len(cmp_list) < max_length:
            cmp_list.append(0)
        return cmp_list
    
    def __cmp__(self, other):
        if isinstance(other, StringType):
            other = MunkiLooseVersion(other)
        
        max_length = max(len(self.version), len(other.version))
        self_cmp_version = self._pad(self.version, max_length)
        other_cmp_version = self._pad(other.version, max_length)
        
        return cmp(self_cmp_version, other_cmp_version)

# manifest functions

def getManifest(manifest):
    '''Returns a plist dictionary of manifest data'''
    req = urllib2.Request(MANIFESTS_URL + '/' + urllib2.quote(manifest))
    response = urllib2.urlopen(req)
    manifestData = response.read()
    return plistlib.readPlistFromString(manifestData)

def processManifestInstalls(manifest):
    '''Takes a manifest plist and returns a list of all installs from it. Recursively calls itself for includes'''
    installList = list()
    for include in manifest['included_manifests']:
        installList += processManifestInstalls(getManifest(include))
    # Done processing includes, now process installs
    for install in manifest['managed_installs']:
        # Add this to the list of things to precache
        installList.append(str(install))
    return installList

# catalog functions

def getCatalog(catalog):
    '''Takes a catalog name and returns the whole catalog'''
    req = urllib2.Request(CATALOG_URL + '/' + urllib2.quote(catalog))
    response = urllib2.urlopen(req)
    catalogData = response.read()
    return plistlib.readPlistFromString(catalogData)

# Stolen from Munki updatecheck.py

def trimVersionString(version_string):
    """Trims all lone trailing zeros in the version string after major/minor.
    Examples:
      10.0.0.0 -> 10.0
      10.0.0.1 -> 10.0.0.1
      10.0.0-abc1 -> 10.0.0-abc1
      10.0.0-abc1.0 -> 10.0.0-abc1
    """
    if version_string == None or version_string == '':
        return ''
    version_parts = version_string.split('.')
    # strip off all trailing 0's in the version, while over 2 parts.
    while len(version_parts) > 2 and version_parts[-1] == '0':
        del version_parts[-1]
    return '.'.join(version_parts)

def makeCatalogDB(catalogitems):
    """Takes an array of catalog items and builds some indexes so we can
    get our common data faster. Returns a dict we can use like a database"""
    name_table = {}
    pkgid_table = {}
    
    itemindex = -1
    for item in catalogitems:
        itemindex = itemindex + 1
        name = item.get('name', 'NO NAME')
        vers = item.get('version', 'NO VERSION')
        
        if name == 'NO NAME' or vers == 'NO VERSION':
            munkicommon.display_warning('Bad pkginfo: %s', item)
        
        # normalize the version number
        vers = trimVersionString(vers)
        
        # build indexes for items by name and version
        if not name in name_table:
            name_table[name] = {}
        if not vers in name_table[name]:
            name_table[name][vers] = []
        name_table[name][vers].append(itemindex)
        
        # build table of receipts
        for receipt in item.get('receipts', []):
            if 'packageid' in receipt and 'version' in receipt:
                pkg_id = receipt['packageid']
                version = receipt['version']
                if not pkg_id in pkgid_table:
                    pkgid_table[pkg_id] = {}
                if not version in pkgid_table[pkg_id]:
                    pkgid_table[pkg_id][version] = []
                pkgid_table[pkg_id][version].append(itemindex)
    
    # build table of update items with a list comprehension --
    # filter all items from the catalogitems that have a non-empty
    # 'update_for' list
    updaters = [item for item in catalogitems if item.get('update_for')]
    
    # now fix possible admin errors where 'update_for' is a string instead
    # of a list of strings
    for update in updaters:
        if isinstance(update['update_for'], basestring):
            # convert to list of strings
            update['update_for'] = [update['update_for']]
    
    # build table of autoremove items with a list comprehension --
    # filter all items from the catalogitems that have a non-empty
    # 'autoremove' list
    # autoremove items are automatically removed if they are not in the
    # managed_install list (either directly or indirectly via included
    # manifests)
    autoremoveitems = [item.get('name') for item in catalogitems if item.get('autoremove')]
    # convert to set and back to list to get list of unique names
    autoremoveitems = list(set(autoremoveitems))
    
    pkgdb = {}
    pkgdb['named'] = name_table
    pkgdb['receipts'] = pkgid_table
    pkgdb['updaters'] = updaters
    pkgdb['autoremoveitems'] = autoremoveitems
    pkgdb['items'] = catalogitems
    
    return pkgdb

CATALOG = {}
def getCatalogs(cataloglist):
    """Retrieves the catalogs from the server and populates our catalogs
    dictionary.
    """
    for catalogname in cataloglist:
        if not catalogname in CATALOG:
            try:
                catalogdata = getCatalog(catalogname)
            except HTTPError as err:
                print 'Could not retrieve catalog %s from server: %s' % (catalogname, err.code)
            except URLError as err:
                print 'Could not retrieve catalog %s from server: %s' % (catalogname, err.reason)
            else:
                CATALOG[catalogname] = makeCatalogDB(catalogdata)

# item functions
    
def getItemDetail(name, cataloglist, vers=''):
    """Searches the catalogs in list for an item matching the given name.
    If no version is supplied, but the version is appended to the name
    ('TextWrangler--2.3.0.0.0') that version is used.
    If no version is given at all, the latest version is assumed.
    Returns a pkginfo item.
    """
    def compare_version_keys(a, b):
        """Internal comparison function for use in sorting"""
        return cmp(MunkiLooseVersion(b), MunkiLooseVersion(a))
    
    vers = 'latest'
    for catalogname in cataloglist:
        if not catalogname in CATALOG.keys():
        # in case the list refers to a non-existent catalog
            continue
    
        # is name in the catalog?
        if name in CATALOG[catalogname]['named']:
            itemsmatchingname = CATALOG[catalogname]['named'][name]
            indexlist = []
            if vers == 'latest':
                # order all our items, latest first
                versionlist = itemsmatchingname.keys()
                versionlist.sort(compare_version_keys)
                for versionkey in versionlist:
                    indexlist.extend(itemsmatchingname[versionkey])
            
            elif vers in itemsmatchingname:
                # get the specific requested version
                indexlist = itemsmatchingname[vers]
            
            for index in indexlist:
                item = CATALOG[catalogname]['items'][index]
                # we have an item whose name and version matches the request.
                return item
    
    # if we got this far, we didn't find it.
    return None
    
def getItemURL(item):
    '''Takes an item dict from getItemDetail() and returns the URL it can be downloaded from'''
    return PKGS_URL + '/' + urllib2.quote(item['installer_item_location'])
    
def downloadURLToCache(URL):
    '''Takes a URL and downloads it to /Library/Managed Installs/Cache'''
    file = urllib2.urlopen(url)
    data = file.read()
    cachePath = os.path.join('{{target_volume}}', 'Library/Managed Installs/Cache')
    with open(cachePath, "wb") as code:
        code.write(data)

def main():
    # at some point, this will be done more automatically - for now I'm cheating
    # by hardcoding the catalog names
    # Populate the CATALOG global
    getCatalogs(['release', 'testing'])
    installList = list()
    # Find the manifest for this current machine
    manifest = getManifest('{{serial_number}}')
    installList = processManifestInstalls(manifest)
    for item in installList:
        print getItemURL(item)

if __name__ == '__main__':
    main()