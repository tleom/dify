"""Run inside Debian's Python so LibreOffice's matching UNO bridge is available."""
import json
import os
from contextlib import chdir
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import uuid

import uno
from com.sun.star.beans import PropertyValue
from com.sun.star.document.MacroExecMode import NEVER_EXECUTE
from com.sun.star.document.UpdateDocMode import NO_UPDATE


def prop(name, value):
    return PropertyValue(Name=name, Value=value)


def convert(source, target, action):
    source, target = Path(source).resolve(), Path(target).resolve()
    if source == target:
        raise ValueError('Office conversion requires a separate output file')
    if not source.is_file():
        raise FileNotFoundError(source)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise FileExistsError(target)
    pipe = 'office_' + uuid.uuid4().hex
    with tempfile.TemporaryDirectory(prefix='workbench-office-') as profile, chdir(profile):
        # Both UNO processes resolve OSL_SOCKET_PATH relative to this private
        # directory. A relative path also avoids AF_UNIX's 108-byte limit with
        # the conversation UUID and LibreOffice's long internal pipe names.
        # The Debian oosplash launcher has its own hard-coded /tmp IPC lookup.
        # Headless workers use the installed binary directly, with explicit
        # bootstrap configuration for the private pipe path.
        command = ['/usr/lib/libreoffice/program/soffice.bin', '-env:OSL_SOCKET_PATH=.', f'-env:UserInstallation={Path(profile).as_uri()}', '--headless', '--norestore', '--nodefault', '--nofirststartwizard', f'--accept=pipe,name={pipe};urp;StarOffice.ServiceManager']
        with tempfile.TemporaryFile() as log:
            process = subprocess.Popen(command, stdout=log, stderr=log)
            document = None
            desktop = None
            try:
                context = uno.getComponentContext()
                resolver = context.ServiceManager.createInstanceWithContext('com.sun.star.bridge.UnoUrlResolver', context)
                deadline = time.monotonic() + 30
                restarted = False
                while True:
                    try:
                        remote = resolver.resolve(f'uno:pipe,name={pipe};urp;StarOffice.ComponentContext')
                        break
                    except Exception:
                        # Fresh Debian profiles request one normal restart (81)
                        # after extension registration; oosplash normally owns it.
                        if process.poll() == 81 and not restarted and time.monotonic() < deadline:
                            restarted = True
                            process = subprocess.Popen(command, stdout=log, stderr=log)
                            continue
                        if process.poll() is not None or time.monotonic() >= deadline:
                            log.seek(0)
                            detail = log.read(2000).decode('utf-8', errors='replace').strip()
                            raise RuntimeError(f'LibreOffice did not become ready (exit={process.returncode})' + (': ' + detail if detail else ''))
                        time.sleep(0.2)
                desktop = remote.ServiceManager.createInstanceWithContext('com.sun.star.frame.Desktop', remote)
                document = desktop.loadComponentFromURL(source.as_uri(), '_blank', 0, (
                    prop('Hidden', True), prop('MacroExecutionMode', NEVER_EXECUTE), prop('UpdateDocMode', NO_UPDATE),
                ))
                if document is None:
                    raise RuntimeError('LibreOffice could not open the document')
                if action == 'recalc':
                    if not document.supportsService('com.sun.star.sheet.SpreadsheetDocument'):
                        raise ValueError('Formula recalculation requires a spreadsheet')
                    document.calculateAll()
                elif action == 'accept-changes':
                    if not document.supportsService('com.sun.star.text.TextDocument'):
                        raise ValueError('Tracked changes require a Word document')
                    dispatcher = remote.ServiceManager.createInstanceWithContext('com.sun.star.frame.DispatchHelper', remote)
                    dispatcher.executeDispatch(document.getCurrentController().getFrame(), '.uno:AcceptAllTrackedChanges', '', 0, ())
                    if document.getRedlines().getCount():
                        raise RuntimeError('LibreOffice left unapplied tracked changes')
                filters = {
                    '.docx': 'Office Open XML Text', '.xlsx': 'Calc MS Excel 2007 XML',
                    '.pptx': 'Impress MS PowerPoint 2007 XML', '.odt': 'writer8',
                    '.ods': 'calc8', '.odp': 'impress8',
                }
                if target.suffix.lower() == '.pdf':
                    filters['.pdf'] = (
                        'calc_pdf_Export' if document.supportsService('com.sun.star.sheet.SpreadsheetDocument')
                        else 'impress_pdf_Export' if document.supportsService('com.sun.star.presentation.PresentationDocument')
                        else 'writer_pdf_Export'
                    )
                filter_name = filters.get(target.suffix.lower())
                if not filter_name:
                    raise ValueError('Unsupported output format')
                document.storeToURL(target.as_uri(), (prop('FilterName', filter_name), prop('Overwrite', False)))
                document.close(True)
                document = None
                if not target.is_file() or target.stat().st_size == 0:
                    raise RuntimeError('LibreOffice produced no output')
                return {'status': 'success', 'output': str(target), 'bytes': target.stat().st_size}
            finally:
                if document is not None:
                    try:
                        document.close(True)
                    except Exception:
                        pass
                if desktop is not None:
                    try:
                        desktop.terminate()
                    except Exception:
                        pass
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.terminate()
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()


if __name__ == '__main__':
    try:
        # Preload is scoped to this worker and its soffice child. The image and
        # other Shell processes retain their normal libc and Landlock behavior.
        shim = str(Path(__file__).with_name('private_ipc.so').resolve())
        if os.environ.get('WORKBENCH_OFFICE_PRIVATE_IPC') != shim:
            if not Path(shim).is_file():
                raise RuntimeError('Office private IPC support is missing from the sandbox image')
            environment = dict(os.environ, WORKBENCH_OFFICE_PRIVATE_IPC=shim,
                               OSL_SOCKET_PATH='.', LD_PRELOAD=shim)
            os.execve(sys.executable, [sys.executable, *sys.argv], environment)
        print(json.dumps(convert(*sys.argv[1:4]), ensure_ascii=False))
    except Exception as error:
        print(json.dumps({'status': 'failed', 'error': str(error)}, ensure_ascii=False))
        raise SystemExit(1)
