from
flask
import
Flask
from
flask_sqlalchemy
import
SQLAlchemy
import
os
app.config[SQLALCHEMY_DATABASE_URI]
=
os.getenv
DATABASE_URL
postgresql://localhost/nitro_bot
app.config[SQLALCHEMY_TRACK_MODIFICATIONS]
=
False
try:
db.session.execute
ALTER TABLE users ALTER COLUMN username TYPE VARCHAR(256)
print
Successfully altered username column to VARCHAR(256)
except
Exception
as
e:

