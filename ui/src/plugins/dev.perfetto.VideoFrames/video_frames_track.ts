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

import {SliceTrack} from '../../components/tracks/slice_track';
import type {Trace} from '../../public/trace';
import {SourceDataset} from '../../trace_processor/dataset';
import {LONG, NUM, STR} from '../../trace_processor/query_result';
import {VideoFrameDetailsPanel} from './video_frame_details_panel';
import type {VideoFramePlayer} from './video_frame_player';

export function createVideoFramesTrack(
  trace: Trace,
  uri: string,
  displayId: number,
  player: VideoFramePlayer,
) {
  // is_config rows are decoder-setup pseudo-frames; we don't want them on the
  // timeline. The slice track displays each AU as a zero-duration event.
  const src = `
    SELECT
      id,
      ts,
      0 AS dur,
      'Frame ' || frame_number AS name
    FROM android_video_frames
    WHERE display_id = ${displayId}
      AND COALESCE(is_config, 0) = 0
  `;
  // Singleton panel: returning the same instance on every selection lets
  // mithril patch the DOM in place rather than remount the canvas. Without
  // this, every selectTrackEvent from the play loop unmounts the canvas
  // (-> detachCanvas -> stop()) and kills playback after one frame, plus
  // causes visible flicker on every selection change.
  const panel = new VideoFrameDetailsPanel(player);
  return SliceTrack.create({
    trace,
    uri,
    dataset: new SourceDataset({
      schema: {id: NUM, ts: LONG, dur: LONG, name: STR},
      src,
    }),
    detailsPanel: () => panel,
  });
}
