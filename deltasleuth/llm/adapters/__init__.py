"""Wire adapters: one module per dialect, named after it, each exposing ``Adapter`` and declaring, as
literals, ``DIALECT`` (its name), ``AUTH`` (the header a credential is sent in, and its scheme) and
``LISTED`` (its place where the known dialects are named). A new dialect is one more module: the profiles
read the dialects from this package (``profiles.DIALECTS``). ``base`` holds what the adapters share."""
