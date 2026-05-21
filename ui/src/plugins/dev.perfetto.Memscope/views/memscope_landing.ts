// Copyright (C) 2026 The Android Open Source Project
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//      http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

import m from 'mithril';
import {QuerySlot} from '../../../base/query_slot';
import {Time, type time} from '../../../base/time';
import {Timestamp} from '../../../components/widgets/timestamp';
import type {Trace} from '../../../public/trace';
import {
  LONG,
  NUM,
  NUM_NULL,
  STR,
  STR_NULL,
} from '../../../trace_processor/query_result';
import {Anchor} from '../../../widgets/anchor';
import {Button} from '../../../widgets/button';

interface HeapDumpRow {
  ts: time;
  upid: number;
  pid: number;
  processName: string;
  eventId: number;
  eventType: string;
}

interface HeapProfileRow {
  ts: time;
  upid: number;
  pid: number;
  processName: string;
  heapName: string;
  samples: number;
  totalSize: number;
  eventId: number;
  eventType: string;
}

interface Data {
  heapDumps: HeapDumpRow[];
  heapProfiles: HeapProfileRow[];
}

type SnapshotEntry =
  | {kind: 'dump'; row: HeapDumpRow}
  | {kind: 'profile'; row: HeapProfileRow};

async function loadData(trace: Trace): Promise<Data> {
  const heapDumps: HeapDumpRow[] = [];
  const dumpRes = await trace.engine.query(`
    SELECT
      e.id AS event_id,
      e.type AS event_type,
      e.ts AS ts,
      e.upid AS upid,
      p.pid AS pid,
      coalesce(p.cmdline, p.name, '<unknown>') AS pname
    FROM heap_profile_events e
    JOIN process p USING (upid)
    WHERE e.type = 'java_heap_graph'
    ORDER BY e.ts ASC
  `);
  for (
    const it = dumpRes.iter({
      event_id: NUM,
      event_type: STR,
      ts: LONG,
      upid: NUM,
      pid: NUM_NULL,
      pname: STR,
    });
    it.valid();
    it.next()
  ) {
    heapDumps.push({
      ts: Time.fromRaw(it.ts),
      upid: it.upid,
      pid: it.pid ?? 0,
      processName: it.pname,
      eventId: it.event_id,
      eventType: it.event_type,
    });
  }

  const heapProfiles: HeapProfileRow[] = [];
  const profRes = await trace.engine.query(`
    SELECT
      MIN(a.id) AS event_id,
      'heap_profile:' || a.heap_name AS event_type,
      a.ts AS ts,
      a.upid AS upid,
      p.pid AS pid,
      coalesce(p.cmdline, p.name, '<unknown>') AS pname,
      a.heap_name AS heap_name,
      COUNT(*) AS samples,
      SUM(CASE WHEN a.size > 0 THEN a.size ELSE 0 END) AS total_size
    FROM heap_profile_allocation a
    JOIN process p USING (upid)
    GROUP BY a.ts, a.upid, a.heap_name
    ORDER BY a.ts ASC
  `);
  for (
    const it = profRes.iter({
      event_id: NUM,
      event_type: STR,
      ts: LONG,
      upid: NUM,
      pid: NUM_NULL,
      pname: STR,
      heap_name: STR_NULL,
      samples: NUM,
      total_size: NUM,
    });
    it.valid();
    it.next()
  ) {
    heapProfiles.push({
      ts: Time.fromRaw(it.ts),
      upid: it.upid,
      pid: it.pid ?? 0,
      processName: it.pname,
      heapName: it.heap_name ?? 'malloc',
      samples: it.samples,
      totalSize: it.total_size,
      eventId: it.event_id,
      eventType: it.event_type,
    });
  }

  return {heapDumps, heapProfiles};
}

function trackUriFor(upid: number, type: string): string {
  return `/process_${upid}/${type}_heap_profile`;
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  const units = ['KiB', 'MiB', 'GiB'];
  let v = bytes / 1024;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i++;
  }
  return `${v.toFixed(2)} ${units[i]}`;
}

function getSortedSnapshots(data: Data): SnapshotEntry[] {
  const entries: SnapshotEntry[] = [
    ...data.heapDumps.map((row): SnapshotEntry => ({kind: 'dump', row})),
    ...data.heapProfiles.map((row): SnapshotEntry => ({kind: 'profile', row})),
  ];
  entries.sort((a, b) => {
    if (a.row.ts < b.row.ts) return -1;
    if (a.row.ts > b.row.ts) return 1;
    return 0;
  });
  return entries;
}

export class MemscopeLandingPage implements m.ClassComponent<{trace: Trace}> {
  private readonly dataSlot = new QuerySlot<Data>();

  onremove() {
    this.dataSlot.dispose();
  }

  view({attrs}: m.Vnode<{trace: Trace}>) {
    const {trace} = attrs;
    let result;
    let error: string | undefined;
    try {
      result = this.dataSlot.use({
        key: {traceId: trace.traceInfo.uuid},
        queryFn: () => loadData(trace),
      });
    } catch (e) {
      error = String(e);
    }
    return m(
      '.pf-memscope-landing',
      {
        style: {
          padding: '24px',
          overflow: 'auto',
          height: '100%',
        },
      },
      m('h1', 'Memscope Trace'),
      m(
        'p',
        {style: {color: 'var(--pf-color-text-muted, #666)'}},
        'Heap dumps and heap profile snapshots captured in this trace.',
      ),
      error && m('p.pf-error', `Error: ${error}`),
      !error && result?.data === undefined && m('p', 'Loading…'),
      result?.data && this.renderSections(trace, result.data),
    );
  }

  private renderSections(trace: Trace, data: Data): m.Children {
    if (data.heapDumps.length === 0 && data.heapProfiles.length === 0) {
      return m(
        '.pf-memscope-landing__empty',
        {style: {marginTop: '24px', fontStyle: 'italic'}},
        'No heap dumps or heap profile snapshots found in this trace.',
      );
    }

    const combined = getSortedSnapshots(data);

    return m(
      'section',
      {style: {marginTop: '24px'}},
      m(
        'table.pf-memscope-landing__table',
        {style: tableStyle},
        m(
          'thead',
          m(
            'tr',
            m('th', {style: thStyle}, 'Time'),
            m('th', {style: thStyle}, 'Type'),
            m('th', {style: thStyle}, 'Process'),
            m('th', {style: thStyle}, 'PID'),
            m('th', {style: thStyle}, 'Heap'),
            m('th', {style: thStyleNum}, 'Samples'),
            m('th', {style: thStyleNum}, 'Total size'),
            m('th', {style: thStyle}, ''),
            m('th', {style: thStyle}, ''),
          ),
        ),
        m(
          'tbody',
          combined.map((entry) => {
            const r = entry.row;
            const viewOnTimeline = m(Button, {
              label: 'View on timeline',
              icon: 'timeline',
              onclick: () => {
                const uri = trackUriFor(r.upid, r.eventType);
                trace.selection.selectTrackEvent(uri, r.eventId, {
                  scrollToSelection: true,
                });
                trace.navigate('#!/viewer');
              },
            });
            if (entry.kind === 'dump') {
              return m(
                'tr',
                m('td', {style: tdStyle}, m(Timestamp, {trace, ts: r.ts})),
                m('td', {style: tdStyle}, 'Java heap dump'),
                m('td', {style: tdStyle}, r.processName),
                m('td', {style: tdStyle}, r.pid),
                m('td', {style: tdStyle}, ''),
                m('td', {style: tdStyleNum}, ''),
                m('td', {style: tdStyleNum}, ''),
                m('td', {style: tdStyle}, viewOnTimeline),
                m(
                  'td',
                  {style: tdStyle},
                  m(
                    Anchor,
                    {href: `#!/heapdump?upid=${r.upid}&ts=${r.ts}`},
                    'Open in Heap Dump Explorer',
                  ),
                ),
              );
            }
            const profile = entry.row;
            return m(
              'tr',
              m('td', {style: tdStyle}, m(Timestamp, {trace, ts: profile.ts})),
              m('td', {style: tdStyle}, 'Native heap profile'),
              m('td', {style: tdStyle}, profile.processName),
              m('td', {style: tdStyle}, profile.pid),
              m('td', {style: tdStyle}, profile.heapName),
              m('td', {style: tdStyleNum}, profile.samples.toLocaleString()),
              m('td', {style: tdStyleNum}, formatBytes(profile.totalSize)),
              m('td', {style: tdStyle}, viewOnTimeline),
              m('td', {style: tdStyle}, ''),
            );
          }),
        ),
      ),
    );
  }
}

const tableStyle = {
  borderCollapse: 'collapse',
  marginTop: '8px',
  width: '100%',
  maxWidth: '1000px',
};

const thStyle = {
  textAlign: 'left',
  padding: '6px 12px',
  borderBottom: '1px solid var(--pf-color-border, #ccc)',
  fontWeight: '600',
};

const thStyleNum = {...thStyle, textAlign: 'right'};

const tdStyle = {
  padding: '6px 12px',
  borderBottom: '1px solid var(--pf-color-border-faint, #eee)',
  verticalAlign: 'baseline',
};

const tdStyleNum = {
  ...tdStyle,
  textAlign: 'right',
  fontVariantNumeric: 'tabular-nums',
};
