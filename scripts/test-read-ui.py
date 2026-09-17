#!/usr/bin/env python3
"""Exercise shipping QML badge/optimistic-read bindings against synthetic data.
Requires Qt6 qmltestrunner; never starts Blip or contacts the Mac.
"""
from pathlib import Path
import os
import shutil
import subprocess
import tempfile
import sys
source = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[1] / "BarWidget.qml"
s = source.read_text()
def fn(name):
 a=s.index('  function '+name+'(');b=s.index('\n  }',a)+4
 return s[a:b]
props=[]
for name in ['threadsJson']:
 props.append(next(l for l in s.splitlines() if 'property string '+name+':' in l))
a=s.index('            var j = JSON.stringify(list)');b=s.index('            root.healthy',a)
update=s[a:b]
out='''import QtQuick
import QtTest
Item {
 id: root
 property var threads: []
 property var localReads: ({})
 property var localUnreads: ({})
 property int unread: 0
 PROPS
 function refresh() {}
 FUNCTIONS
 function poll(list) {
 UPDATE
 }
 TestCase {
  name: "BlipBadge"
  function init() { root.threads = []; root.localReads = ({}); root.localUnreads = ({}); root.unread = 0 }
  function test_read_then_stale_poll() {
   var original = [{chat:"A",unread:1,last_ts:"2026-09-01T10:00:00Z"},{chat:"B",unread:1,last_ts:"2026-09-01T10:00:00Z"},{chat:"C",unread:1,last_ts:"2026-09-01T10:00:00Z"}]
   root.poll(original)
   compare(root.unread, 3)
   root.markThreadRead("A", "2026-09-01T10:00:00Z", "")
   compare(root.unread, 2)
   // Force the old poll result through: whatever policy chooses for the dot,
   // list and count must ALWAYS describe the same snapshot.
   root.poll(original)
   compare(root.unread, root.unreadChatCount(root.threads))
   compare(root.threads.filter(function(t) {return t.unread>0}).length, 3)
  }
  function test_optimistic_read_filters_inflight_poll() {
   var original = [{chat:"A",unread:1,last_ts:"2026-09-01T10:00:00Z"}]
   root.poll(original)
   root.markThreadRead("A", "2026-09-01T10:00:00Z", "")
   root.poll(root.applyLocalReads(original))
   compare(root.unread, 0)
   compare(root.threads[0].unread, 0)
   var newer = [{chat:"A",unread:1,last_ts:"2026-09-01T11:00:00Z"}]
   root.poll(root.applyLocalReads(newer))
   compare(root.unread, 1)
  }
  function test_unread_then_stale_poll() {
   var original = [{chat:"A",unread:0,last_ts:"2026-09-01T10:00:00Z"}]
   root.poll(original)
   root.markThreadUnread("A")
   root.poll(root.applyLocalReads(original))
   compare(root.unread, 1)
   compare(root.threads[0].unread, 1)
  }
 }
}
'''.replace('PROPS','\n'.join(props)).replace('FUNCTIONS','\n'.join(fn(n) for n in ['unreadChatCount','noteLocalRead','noteLocalUnread','applyLocalReads','markThreadRead','markThreadUnread'])).replace('UPDATE',update)
runner = shutil.which("qmltestrunner") or "/usr/lib/qt6/bin/qmltestrunner"
with tempfile.TemporaryDirectory(prefix="blip-read-ui-") as path:
    (Path(path) / "tst_badge.qml").write_text(out)
    result = subprocess.run([runner, "-input", path, "-platform", "offscreen"],
        env={**os.environ, "QT_QPA_PLATFORM": "offscreen", "QT_QPA_PLATFORMTHEME": "generic"})
    sys.exit(result.returncode)
