PyTags
======
This package provides Python source code indexing and navigation in
[Sublime Text 2](http://sublimetext.com). You can also enable import
completions.

Quick Start
-----------
Install the package and add to your settings:

```json
"pytags_index_on_load": true,
"pytags_index_on_save": true,
"pytags_databases":
    [
        {
            "include_project_folders": true,
            "path": "$HOME/.pytags.db"
        }
    ]
```

Now all Python files that you open or that are in your project folders will get
indexed.

Requirements
------------
Since SQLite is used to store indexed data, external python interpreter is
used (Sublime's embedded interpreter doesn't ship with sqlite module). Just
make sure that
```shell
python -u some_script.py
```
works. I tested it with Python 2.7, but I expect it to work with Python >= 2.6,
3.x included.

Command Palette
---------------

 - **Update Index** - Indexes files that have changed since last scan.
 - **Rebuild Index** - Indexes all files.
 - **Find Definition** - Skips to specified symbol definition.

Key Bindings
-------------
Ctrl+T, Ctrl+R updates the index. Ctrl+T, Ctrl+T skips to definition of symbol
under cursor.

Settings
--------

 - **pytags\_index\_on\_save** - Whether to index saved files.
 - **pytags\_index\_on\_load** - Whether to index opened files.
 - **pytags\_complete\_imports** - Whether to enable import completions.
 - **pytags\_databases** - List of databases to search/update, see next section.

Database Definitions
--------------------
You can use multiple databases at a time. This allows indexing of project
related stuff in one place and site packages in another, sharing some databases
among different projects. Each database definition requires "path" field (path
to the SQLite database file), and may include any of the following fields:

 - **include\_project\_folders** - Whether this database should index files
                                   found in project folders.
 - **roots** - List of directories containing files that should be indexed.
 - **pattern** - Regular expression that each indexed file should match.

All file paths can contain environment variables expandable with Python's
_os.path.expandvars_.


Import Completions
------------------
Only single components of module/package are included in completions (not full
_some.module.name_ paths). Import completions are displayed with "Package"
annotation on the right.
