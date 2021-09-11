import sqlite3
from os import urandom
from base64 import b64decode
from base58 import b58encode
from nacl.public import PrivateKey, SealedBox
from threading import Lock


class DB:
	
	def __init__(self, filepath, private_key_bytes, id_len_bytes):
		self._id_len_bytes = id_len_bytes
		self._write_mutex = Lock()
		self._db = sqlite3.connect(filepath, check_same_thread=False)
		self._db.row_factory = sqlite3.Row
		with self._db as sql: sql.execute('PRAGMA foreign_keys = ON')
		self.ensure_schema()
		private_key = PrivateKey(private_key_bytes)
		self._crypto = SealedBox(private_key)
		self._public_key = private_key.public_key

	def ensure_schema(self):
		with self._write_mutex, self._db as sql:
			sql.execute("""
				CREATE TABLE IF NOT EXISTS admins (
					id INTEGER PRIMARY KEY AUTOINCREMENT, 
					user_type TEXT NOT NULL, 
					user_id TEXT NOT NULL, 
					active BOOLEAN NOT NULL,
					UNIQUE (user_type, user_id)
				)
			""")
			sql.execute("""
				CREATE TABLE IF NOT EXISTS templates (
					id TEXT NOT NULL PRIMARY KEY, 
					content TEXT NOT NULL
				)
			""")
			sql.execute("""
				CREATE TABLE IF NOT EXISTS batches (
					id TEXT NOT NULL PRIMARY KEY, 
					admin_id INTEGER NOT NULL,
					created TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
					closed TIMESTAMP,
					FOREIGN KEY (admin_id) REFERENCES admins (id)
				)
			""")
			sql.execute("""
				CREATE TABLE IF NOT EXISTS items (
					id INTEGER PRIMARY KEY AUTOINCREMENT, 
					batch_id TEXT NOT NULL,
					target_type TEXT NOT NULL, 
					target_id TEXT NOT NULL, 
					category TEXT NOT NULL,
					data TEXT NOT NULL,
					template_id TEXT NOT NULL,
					created TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
					archived BOOLEAN NOT NULL DEFAULT FALSE,
					FOREIGN KEY (batch_id) REFERENCES batches (id),
					FOREIGN KEY (template_id) REFERENCES templates (id)
				)
			""")
			sql.execute("""
				CREATE INDEX IF NOT EXISTS items_by_target ON items (target_type, target_id, category);
			""")

	def generate_uid(self):
		return b58encode(urandom(self._id_len_bytes)).decode('utf-8')
		
	def is_admin(self, user_type, user_id):
		with self._db as sql:
			res = sql.execute("""
				SELECT 
					id FROM admins 
				WHERE 
					user_type = :user_type 
					AND user_id = :user_id 
					AND active = TRUE
			""", dict(user_type=user_type, user_id=user_id))
			row = res.fetchone()
			return row['id'] if row else None

	def make_admin(self, user_type, user_id):
		with self._write_mutex, self._db as sql:
			sql.execute("""
				INSERT OR REPLACE INTO admins (user_type, user_id, active) VALUES (:user_type, :user_id, TRUE)
			""", dict(user_type=user_type, user_id=user_id))
			return True
	
	def revoke_admin(self, user_type, user_id):
		with self._write_mutex, self._db as sql:
			sql.execute("""
				INSERT OR REPLACE INTO admins (user_type, user_id, active) VALUES (:user_type, :user_id, FALSE)
			""", dict(user_type=user_type, user_id=user_id))
			return True	

	def create_or_check_batch(self, admin, batch=None):
		if batch is None:
			with self._write_mutex, self._db as sql:
				batch = self.generate_uid()
				sql.execute("""
					INSERT INTO batches (id, admin_id) VALUES (:id, :admin_id)
				""", dict(id=batch, admin_id=admin))
				return batch
		else:
			with self._db as sql:
				res = sql.execute("""
					SELECT
						COUNT(*) FROM batches
					WHERE
						id = :batch_id
						AND admin_id = :admin_id
						AND closed IS NULL
				""", dict(admin_id=admin, batch_id=batch))
				if res.fetchone()[0] == 0: raise Exception(f'admin ({admin}) has no batch: {batch}')
				return batch

	def close_batch(self, batch):
		with self._write_mutex, self._db as sql:
			sql.execute("""
				UPDATE batches SET closed = CURRENT_TIMESTAMP WHERE id = :batch_id
			""", dict(batch_id=batch))
		
	def decrypt_data(self, ciphertext):
		try:
			return self._crypto.decrypt(ciphertext=b64decode(ciphertext)).decode('utf-8')
		except:
			return None

	def add_item(self, batch, target_type, target_id, category, data_encrypted_b64, template):
		if self.decrypt_data(data_encrypted_b64) is None: return False
		with self._write_mutex, self._db as sql:
			try:
				sql.execute("""
					INSERT 
						INTO items (batch_id, target_type, target_id, category, data, template_id) 
						VALUES (:batch_id, :target_type, :target_id, :category, :data, :template_id)
				""", dict(batch_id=batch, target_type=target_type, target_id=target_id, category=category, data=data_encrypted_b64, template_id=template))
				return True
			except sqlite3.IntegrityError as e:
				return False
	
	def get_items(self, target_type, target_id):
		with self._db as sql:
			res = sql.execute("""
				SELECT 
					category, data, template_id FROM items 
				WHERE
					target_type = :target_type 
					AND target_id = :target_id
					AND archived = FALSE
				ORDER BY
					created DESC
			""", dict(target_type=target_type, target_id=target_id))
		rows = res.fetchall()
		decrypted_rows = [{ 'data': self.decrypt_data(r['data']), 'template_id': r['template_id'] } for r in rows]
		return list(filter(lambda x: x['data'] is not None, decrypted_rows))

	def get_template(self, template):
		with self._db as sql:
			res = sql.execute("""
				SELECT
					content FROM templates
				WHERE
					id = :template_id
			""", dict(template_id=template))
		rows = res.fetchone()
		return rows['content'] if len(rows) > 0 else None

	def get_public_key(self):
		return self._public_key
