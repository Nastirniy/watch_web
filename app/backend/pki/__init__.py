"""Client certificate library: profiles, the CA checks, the .p12 package, the CRL and the certificate folder.

Pure functions over `cryptography` and the file system: no database, no FastAPI, and never `app.backend.config`
(the signing service runs without the settings of the app).
"""
