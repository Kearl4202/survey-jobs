SURVEY JOBS APP
===============

A simple web app to manage Trimble JXL survey files and export KMZ.

HOW TO RUN
----------
Windows:  Double-click START.bat
Mac/Linux: Run ./start.sh in Terminal

Then open your browser to: http://localhost:5000

Anyone on your network can also open it at:
  http://YOUR-COMPUTER-IP:5000

FIRST TIME SETUP
----------------
1. Install Python from https://www.python.org/downloads/
   (check "Add Python to PATH")
2. Double-click START.bat
3. Open http://localhost:5000 in your browser

HOW IT WORKS
------------
- Upload a JXL file -> it auto-creates a project from the first 9
  characters of the filename (e.g. "080_89725")
- Upload another JXL with the same first 9 chars -> adds a new
  job card inside the same project
- Each job card shows: date, point count, line names, feature badges
- "KMZ" button on each card -> downloads just that job
- "Full KMZ" button on project header -> downloads all jobs merged

PROJECT STRUCTURE IN KMZ
-------------------------
Project folder (080_89725)
  └── Job folder (080_89725-033026-KE)
        └── WELD MAP - COATING (12)
              └── point 120756, 120757...
        └── FITTINGS (4)
        └── FOREIGN PL (4)
        └── MISC (1)

DATA IS SAVED
-------------
All uploaded data is stored in the data/projects.json file.
It persists between restarts — you don't lose anything.

COORDINATE SYSTEMS
------------------
The app automatically reads the coordinate system from each JXL file.
Tested with Texas North Central (EPSG 6583). Works with any
projected coordinate system that pyproj supports.
