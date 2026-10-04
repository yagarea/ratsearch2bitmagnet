"""Import a Rats Search (rats-search) torrent index into bitmagnet.

Sources (what is read, ``sources/``):
  * a ``.ratsdb`` dump written by rats-search 2.x (needs no extra packages);
  * the Manticore Search instance behind a running rats-search (``host:port``);
  * a rats-search data directory: attaches to the instance named in its
    ``sphinx.conf`` or starts a private ``searchd`` on the on-disk tables.

Sinks (where it is written, ``sinks/``):
  * bitmagnet's HTTP ``/import`` endpoint: names, sizes, dates and content-type
    hints. The endpoint takes no file lists;
  * bitmagnet's PostgreSQL database directly: everything above plus the file
    lists, and a classification job per batch, as bitmagnet's own importer does;
  * JSON lines in the ``/import`` format, to a file or stdout.

``model`` holds the torrent entity and the content-type mapping, ``items`` the
``/import`` item format shared by the sinks, and ``cli`` the command line.
"""

__version__ = "0.1.0"
