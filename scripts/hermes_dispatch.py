"""Copy as a regular ~/.hermes/scripts/hermes_collect.py for release-based installs."""
from pathlib import Path
import runpy

if __name__ == '__main__':
    runpy.run_path(str(Path.home() / 'job_search/current/scripts/hermes_collect.py'), run_name='__main__')
