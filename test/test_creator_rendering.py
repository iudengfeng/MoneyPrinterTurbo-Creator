from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
from PIL import Image

from app.services.creator import extract, rendering, store


class CreatorRenderingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.assets = tempfile.TemporaryDirectory()
        cls.asset_root = Path(cls.assets.name)
        cls.ffmpeg = extract.ffmpeg_binary()
        cls.video = cls.asset_root / 'source.mp4'
        cls.alternate = cls.asset_root / 'alternate.wav'
        cls.music = cls.asset_root / 'short-music.wav'
        cls.silent = cls.asset_root / 'silent.mp4'
        cls.picture = cls.asset_root / 'overlay.png'
        Image.new('RGB', (120, 80), (255, 90, 20)).save(cls.picture)
        def run(args):
            subprocess.run([cls.ffmpeg, '-v', 'error', '-nostdin', '-y', *args], check=True, capture_output=True, timeout=30)
        run(['-f', 'lavfi', '-i', 'color=c=blue:s=160x240:r=30:d=3', '-f', 'lavfi', '-i', 'sine=frequency=440:duration=3:sample_rate=48000', '-c:v', 'libx264', '-preset', 'ultrafast', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-shortest', str(cls.video)])
        run(['-i', str(cls.video), '-an', '-c:v', 'copy', str(cls.silent)])
        run(['-f', 'lavfi', '-i', 'sine=frequency=660:duration=2:sample_rate=48000', str(cls.alternate)])
        run(['-f', 'lavfi', '-i', 'sine=frequency=880:duration=0.25:sample_rate=48000', str(cls.music)])

    @classmethod
    def tearDownClass(cls):
        cls.assets.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.environment = patch.dict(os.environ, {'MPT_CREATOR_DATA': str(self.root / 'creator')})
        self.environment.start()
        self.srt = self.root / 'captions.srt'
        self.srt.write_text('1\n00:00:00,000 --> 00:00:03,000\n这是一段需要换行处理的字幕，文字较长也不应超出视频画面。\n', 'utf-8')

    def tearDown(self):
        self.environment.stop()
        self.temp.cleanup()

    def _render_stub(self, root, request, duration, log, progress):
        payload = json.loads(Path(request).read_text('utf-8'))
        log.write_text('', 'utf-8')
        shutil.copy2(self.silent, payload['output'])

    def _render(self, **kwargs):
        with patch.object(rendering, '_run_hyperframes', side_effect=self._render_stub):
            return rendering.render_video(self.video, **kwargs)

    def _pcm(self, path):
        result = subprocess.run([self.ffmpeg, '-v', 'error', '-i', str(path), '-map', '0:a:0', '-ac', '1', '-ar', '48000', '-f', 'f32le', '-'], check=True, capture_output=True, timeout=30)
        return np.frombuffer(result.stdout, dtype=np.float32)

    def _tone(self, pcm, frequency, start=0.8, duration=0.5):
        segment = pcm[round(start*48000):round((start+duration)*48000)].astype(float)
        clock = np.arange(len(segment)) / 48000
        return abs(np.sum(segment * np.exp(-2j * math.pi * frequency * clock))) * 2 / len(segment)

    def test_probe_actual_width_height_audio_and_reject_invalid_media(self):
        row = rendering.probe_source(self.video)
        self.assertEqual((row['width'], row['height']), (160, 240))
        self.assertTrue(row['has_audio'])
        self.assertAlmostEqual(row['duration'], 3, places=1)
        self.assertFalse(rendering.probe_source(self.alternate)['has_video'])
        broken = self.root / 'broken.mp4'
        broken.write_text('<html>this is not a video</html>')
        with self.assertRaises(ValueError):
            rendering.probe_source(broken)

    def test_original_voice_and_short_music_are_actually_mixed_and_looped(self):
        original_hash = self.video.read_bytes()
        result = self._render(bgm_path=self.music, bgm_volume=0.3)
        pcm = self._pcm(result['video_path'])
        self.assertGreater(self._tone(pcm, 440), 0.10)
        self.assertGreater(self._tone(pcm, 880), 0.02)
        self.assertGreater(self._tone(pcm, 880, start=2.0), 0.02)
        self.assertEqual(self.video.read_bytes(), original_hash)
        self.assertEqual(result['audio_mode'], 'original')
        self.assertEqual(result['state'], 'done')
        self.assertEqual(len(rendering.list_renders()), 1)

    def test_zero_music_volume_preserves_voice_without_music(self):
        result = self._render(bgm_path=self.music, bgm_volume=0)
        pcm = self._pcm(result['video_path'])
        self.assertGreater(self._tone(pcm, 440), 0.10)
        self.assertLess(self._tone(pcm, 880), 0.002)

    def test_replacement_audio_is_used_instead_of_original_voice(self):
        result = self._render(audio_path=self.alternate)
        pcm = self._pcm(result['video_path'])
        self.assertGreater(self._tone(pcm, 660), 0.10)
        self.assertLess(self._tone(pcm, 440), 0.002)
        self.assertEqual(result['audio_mode'], 'replacement')
        self.assertAlmostEqual(result['duration'], 2, places=1)
        self.assertAlmostEqual(rendering.probe_source(result['video_path'])['duration'], 2, places=1)

    def test_short_main_video_rejects_long_audio_before_recognition_or_render(self):
        audio = self.root / 'long.wav'
        subprocess.run([self.ffmpeg, '-v', 'error', '-nostdin', '-y', '-f', 'lavfi', '-i',
                        'sine=frequency=700:duration=8:sample_rate=48000', str(audio)],
                       check=True, capture_output=True, timeout=30)
        original = self.video.read_bytes()
        with patch.object(extract, 'extract_media') as recognition, patch.object(rendering, '_run_hyperframes') as render:
            with self.assertRaisesRegex(ValueError, '不能循环画面'):
                rendering.render_video(self.video, audio_path=audio, auto_subtitles=True)
        recognition.assert_not_called()
        render.assert_not_called()
        self.assertEqual(self.video.read_bytes(), original)
        self.assertEqual(store.list_records('renders'), [])

    def test_long_container_audio_cannot_hide_short_video_track(self):
        source = self.root / 'short-video-long-audio.mp4'
        subprocess.run([self.ffmpeg, '-v', 'error', '-nostdin', '-y', '-i', str(self.video),
                        '-f', 'lavfi', '-i', 'sine=frequency=700:duration=8:sample_rate=48000',
                        '-map', '0:v:0', '-map', '1:a:0', '-c:v', 'copy', '-c:a', 'aac', str(source)],
                       check=True, capture_output=True, timeout=30)
        self.assertGreater(rendering.probe_source(source)['duration'], 7)
        with patch.object(rendering, '_run_hyperframes') as render:
            with self.assertRaisesRegex(ValueError, '人物视频画面只有 3.0 秒'):
                rendering.render_video(source)
        render.assert_not_called()

    def test_music_fades_out_without_truncating_voice(self):
        result = self._render(bgm_path=self.music, bgm_volume=0.3)
        pcm = self._pcm(result['video_path'])
        normal = self._tone(pcm, 880, start=1.0, duration=0.25)
        faded = self._tone(pcm, 880, start=2.75, duration=0.15)
        self.assertLess(faded, normal * 0.6)
        self.assertGreater(self._tone(pcm, 440, start=2.75, duration=0.15), 0.09)

    def test_silent_video_can_get_music_or_remain_silent(self):
        with patch.object(rendering, '_run_hyperframes', side_effect=self._render_stub):
            music = rendering.render_video(self.silent, bgm_path=self.music)
            no_music = rendering.render_video(self.silent)
        self.assertTrue(rendering.probe_source(music['video_path'])['has_audio'])
        self.assertFalse(rendering.probe_source(no_music['video_path'])['has_audio'])

    def test_auto_subtitles_identify_selected_replacement_audio(self):
        with patch.object(extract, 'extract_media', return_value={'srt_path': str(self.srt)}) as recognition:
            result = self._render(audio_path=self.alternate, auto_subtitles=True)
        self.assertEqual(recognition.call_args.args[0], str(self.alternate.resolve()))
        self.assertTrue(result['subtitles_burned'])
        self.assertTrue(Path(result['srt_path']).is_file())
        payload = json.loads((Path(result['video_path']).parent/'request.json').read_text('utf-8'))
        self.assertEqual(rendering.read_srt(result['srt_path']), payload['captions'])
        self.assertEqual(Path(result['source_subtitle_path']), self.srt)
        self.assertEqual(rendering.read_srt(self.srt)[0]['end'], 3)

    def test_subtitle_modes_cannot_duplicate_existing_burned_text(self):
        cases = [dict(auto_subtitles=True, subtitle_path=self.srt), dict(auto_subtitles=True, source_subtitles_burned=True),
                 dict(subtitle_path=self.srt, source_subtitles_burned=True), dict(subtitle_path=self.srt, subtitle_style='none')]
        with patch.object(rendering, '_run_hyperframes') as render:
            for case in cases:
                with self.subTest(case=case), self.assertRaises(ValueError):
                    rendering.render_video(self.video, **case)
        render.assert_not_called()

    def test_subtitles_outside_selected_audio_fail_without_producing_new_final(self):
        self.srt.write_text('1\n00:00:10,000 --> 00:00:12,000\n不在音轨内\n', 'utf-8')
        with self.assertRaisesRegex(ValueError, '没有交集'):
            self._render(subtitle_path=self.srt)
        self.assertEqual(rendering.list_renders(), [])
        self.assertEqual(store.list_records('renders')[0]['state'], 'failed')

    def test_failure_retains_prior_success_and_source_files(self):
        first = self._render()
        contents = Path(first['video_path']).read_bytes()
        with patch.object(rendering, '_run_hyperframes', side_effect=RuntimeError('render stopped')):
            with self.assertRaisesRegex(RuntimeError, 'render stopped'):
                rendering.render_video(self.video, style='bold')
        self.assertEqual(Path(first['video_path']).read_bytes(), contents)
        self.assertEqual([row['id'] for row in rendering.list_renders()], [first['id']])
        self.assertEqual(store.list_records('renders')[0]['state'], 'failed')

    def test_pip_timing_position_and_amount_are_validated(self):
        valid = {'path': str(self.picture), 'start': 0.5, 'end': 2, 'position': 'top-left', 'size': 0.3}
        good = rendering._validate_pip([valid], 3)[0]
        self.assertEqual(good['kind'], 'image')
        self.assertEqual((good['width'], good['height']), (120, 80))
        bad = [dict(valid, start=-1), dict(valid, end=4), dict(valid, start=2, end=1), dict(valid, position='outside'), dict(valid, size=1), dict(valid, end=float('nan'))]
        for item in bad:
            with self.subTest(item=item), self.assertRaises(ValueError):
                rendering._validate_pip([item], 3)
        with self.assertRaises(ValueError):
            rendering._validate_pip([valid]*33, 3)

    def test_pip_video_is_rendered_only_in_requested_time_window(self):
        result = self._render(pip_items=[{'path': str(self.video), 'start': 0.7, 'end': 1.5, 'position': 'bottom-right'}])
        payload = json.loads((Path(result['video_path']).parent/'request.json').read_text('utf-8'))
        item = payload['pipItems'][0]
        self.assertEqual((item['start'], item['end'], item['kind']), (0.7, 1.5, 'video'))
        self.assertTrue(payload['silent'])

    def test_long_cjk_and_latin_captions_fit_two_lines_and_preserve_text(self):
        text = '这是较长的中文字幕，需要分屏显示而不覆盖整个画面。' * 8 + ' Supercalifragilisticexpialidocious is a very long English word.'
        rows = rendering.prepare_captions([{'start': 0, 'end': 10, 'text': text}], 10, 720, 'bold')
        self.assertGreater(len(rows), 3)
        self.assertEqual(rows[0]['start'], 0)
        self.assertEqual(rows[-1]['end'], 10)
        self.assertEqual(''.join(row['text'].replace('\n', '').replace(' ', '') for row in rows), text.replace(' ', ''))
        for row in rows:
            self.assertLessEqual(len(row['text'].splitlines()), 2)
            self.assertTrue(all(rendering._units(line) <= 26 for line in row['text'].splitlines()))

    def test_srt_is_sorted_and_rejects_invalid_clock(self):
        self.srt.write_text('1\n00:00:02,000 --> 00:00:03,000\n后\n\n2\n00:00:00,000 --> 00:00:01,000\n前\n', 'utf-8')
        self.assertEqual([row['text'] for row in rendering.read_srt(self.srt)], ['前', '后'])
        self.srt.write_text('1\n00:99:00,000 --> 01:00:00,000\n坏时间\n', 'utf-8')
        with self.assertRaisesRegex(ValueError, '无效'):
            rendering.read_srt(self.srt)

    def test_invalid_style_volume_audio_and_no_voice_recognition_are_rejected(self):
        for options in [dict(style='fake'), dict(bgm_volume=0.8), dict(bgm_volume=float('nan')), dict(color_grade='unknown'), dict(subtitle_style='unknown')]:
            with self.subTest(options=options), self.assertRaises(ValueError):
                rendering.render_video(self.video, **options)
        with self.assertRaisesRegex(ValueError, '不包含音轨'):
            rendering.render_video(self.video, audio_path=self.silent)
        with self.assertRaisesRegex(ValueError, '没有口播'):
            rendering.render_video(self.silent, auto_subtitles=True)

    def _actual_word_segment(self):
        # Real word timings from the 7.32 s integration clip that exposed the tail bug.
        raw = [(0, .34, '这是'), (.34, .6, '一'), (.6, .74, '段'), (.74, .96, '安'),
               (.96, 1.14, '装'), (1.14, 1.32, '验'), (1.32, 1.54, '证'), (1.54, 1.72, '配'),
               (1.72, 2, '音,'), (2.64, 2.88, '选'), (2.88, 3.08, '题'), (3.08, 3.28, '和'),
               (3.28, 3.46, '文'), (3.46, 3.62, '案'), (3.62, 3.82, '确'), (3.82, 4.06, '认'),
               (4.06, 4.28, '后'), (4.28, 4.84, '就可以'), (4.84, 5.12, '在'), (5.12, 5.46, '这里'),
               (5.46, 5.7, '生'), (5.7, 5.9, '成'), (5.9, 6.08, '完'), (6.08, 6.28, '整'),
               (6.28, 6.5, '配'), (6.5, 6.74, '音。')]
        return {'start': 0, 'end': 6.74, 'text': ''.join(row[2] for row in raw),
                'words': [{'start': start, 'end': end, 'text': text} for start, end, text in raw]}

    def test_real_word_timings_create_complete_natural_short_clauses_without_isolated_tail(self):
        segment = self._actual_word_segment()
        captions = rendering.prepare_captions([segment], 7.32, 1280)
        self.assertEqual(captions, [
            {'start': 0, 'end': 2, 'text': '这是一段安装验证配音,'},
            {'start': 2.64, 'end': 4.84, 'text': '选题和文案确认后就可以'},
            {'start': 4.84, 'end': 6.74, 'text': '在这里生成完整配音。'},
        ])
        self.assertEqual(''.join(row['text'] for row in captions), segment['text'])
        self.assertFalse(any(row['text'] == '音。' for row in captions))
        self.assertTrue(all(len(row['text']) <= 15 and '\n' not in row['text'] for row in captions))
        # The multi-character timed word is displayed as a unit, at its real boundary.
        self.assertTrue(any('就可以' in row['text'] for row in captions))
        boundaries = {row['start'] for row in segment['words']} | {row['end'] for row in segment['words']}
        self.assertTrue(all(row['start'] in boundaries and row['end'] in boundaries for row in captions))

    def test_uploaded_subtitle_without_word_data_balances_tail_and_preserves_punctuation(self):
        segment = self._actual_word_segment()
        segment.pop('words')
        for style in ['clean', 'bold']:
            with self.subTest(style=style):
                captions = rendering.prepare_captions([segment], 7.32, 720, style)
                self.assertEqual(''.join(row['text'] for row in captions), segment['text'])
                self.assertTrue(all(3 <= len(row['text']) <= 15 for row in captions))
                self.assertEqual(captions[0]['text'], '这是一段安装验证配音,')
                self.assertEqual(captions[-1]['end'], 6.74)

    def test_render_exports_final_word_timeline_without_changing_original_srt(self):
        segment = self._actual_word_segment()
        self.srt.write_text('1\n00:00:00,000 --> 00:00:06,740\n' + segment['text'] + '\n', 'utf-8')
        original = self.srt.read_bytes()
        with patch.object(extract, 'extract_media', return_value={'srt_path': str(self.srt), 'segments': [segment]}):
            result = self._render(auto_subtitles=True)
        payload = json.loads((Path(result['video_path']).parent/'request.json').read_text('utf-8'))
        self.assertEqual(rendering.read_srt(result['srt_path']), payload['captions'])
        self.assertEqual(self.srt.read_bytes(), original)
        self.assertEqual(result['source_subtitle_path'], str(self.srt))
        self.assertEqual(payload['captions'][1]['start'], 2.64)
        self.assertEqual(payload['captions'][1]['end'], 3)  # fixture video ends here
        self.assertEqual(payload['captions'][1]['text'], '选题')  # later words are outside this clip
        self.assertFalse(any(row['text'] == '音。' for row in payload['captions']))

    def test_video_fit_defaults_to_complete_frame_and_accepts_explicit_cover(self):
        default = self._render()
        filled = self._render(video_fit='cover')
        for row, expected in [(default, 'contain'), (filled, 'cover')]:
            payload = json.loads((Path(row['video_path']).parent/'request.json').read_text('utf-8'))
            self.assertEqual(payload['videoFit'], expected)
            self.assertEqual(row['video_fit'], expected)
        with patch.object(rendering, '_run_hyperframes') as renderer:
            with self.assertRaisesRegex(ValueError, '适配'):
                rendering.render_video(self.video, video_fit='stretch')
        renderer.assert_not_called()

    def test_presets_and_local_library_are_real_and_do_not_claim_genre(self):
        self.assertEqual({row['id'] for row in rendering.list_presets()}, {'clean', 'bold', 'knowledge', 'business'})
        songs = self.root / 'resource/songs'
        songs.mkdir(parents=True)
        with patch.object(rendering, '__file__', str(self.root / 'app/services/creator/rendering.py')):
            self.assertEqual(rendering.list_bgm(), [])
            track = songs / 'fixture.wav'
            shutil.copy2(self.music, track)
            (songs / 'not-music.txt').write_text('not selectable', 'utf-8')
            music = rendering.list_bgm()
        self.assertEqual(len(music), 1)
        self.assertEqual(music[0]['path'], str(track.resolve()))
        self.assertTrue(rendering.probe_source(music[0]['path'])['has_audio'])
        self.assertTrue(music[0]['name'].startswith('本机音乐 '))
        self.assertNotIn('genre', music[0])


if __name__ == '__main__':
    unittest.main()
