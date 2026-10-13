import csv
import json
import os
from pathlib import Path
import queue
import re
import threading
import tempfile
import time
import tkinter as tk
from tkinter import ttk
import types
import unittest
from unittest import mock

import word_picker


ITEM = {
    "word": "select",
    "seen_form": "selected",
    "phonetic": "/sɪˈlekt/",
    "part_of_speech": "v.",
    "meaning_zh": "选择",
    "usage_zh": "select sth",
    "example": "Select a word.",
    "example_zh": "选择一个单词。",
}


CSV_HEADER = "word,phonetic,definition,translation,pos,collins,oxford,tag,bnc,frq,exchange,detail,audio\n"
CSV_FIELDS = CSV_HEADER.strip().split(",")


# 旧版由 Python 自己写的高光索引位置（高光库/索引文档库/高光记录.md）。高光改成由
# 插件落盘之后，程序不再碰这条路径——下面几条测试拿它当「不该被写」的靶子。
LEGACY_HIGHLIGHT_INDEX_PARTS = ("NoteDataOfY", "高光库", "索引文档库", "高光记录.md")


def write_dictionary(path: Path, rows: list[str]) -> None:
    path.write_text(CSV_HEADER + "\n".join(rows) + "\n", encoding="utf-8")


def write_dictionary_records(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


class FakeApiService:
    def __init__(self, api_key="test-key"):
        self.api_key = api_key
        self.calls: list[list[str]] = []

    def lookup(self, sentence, prepared_words):
        self.calls.append([item["word"] for item in prepared_words])
        return [
            {
                "word": item["word"],
                "seen_form": item["seen_form"],
                "phonetic": "/api/",
                "part_of_speech": "n.",
                "meaning_zh": "接口",
                "usage_zh": "应用程序接口",
                "example": "Use the API.",
                "example_zh": "使用该接口。",
            }
            for item in prepared_words
        ]


class FeatureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Simulated clicks must not pollute the user's operational diagnostic log.
        cls._highlight_log_dir = tempfile.TemporaryDirectory()
        cls._highlight_log_patch = mock.patch.object(
            word_picker, "HIGHLIGHT_LOG_PATH",
            Path(cls._highlight_log_dir.name) / "highlight-events.log",
        )
        cls._highlight_log_patch.start()

    @classmethod
    def tearDownClass(cls):
        cls._highlight_log_patch.stop()
        cls._highlight_log_dir.cleanup()

    def test_highlight_trace_persists_error_without_selection_or_token(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "trace.log"
            with mock.patch.object(word_picker, "HIGHLIGHT_LOG_PATH", path):
                word_picker.record_highlight_event(
                    "bridge_error", capture_id="capture-test", error="连接\n超时"
                )
                entry = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
                self.assertEqual(entry["event"], "bridge_error")
                self.assertEqual(entry["error"], "连接 超时")
                self.assertIn("+", entry["time"])
                path.write_text("x" * (2 * 1024 * 1024), encoding="utf-8")
                word_picker.record_highlight_event("operation_done", overlay_ok=True)
                self.assertTrue(path.with_name("trace.log.1").exists())
                self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["event"],
                                 "operation_done")
            # A bad log path is never allowed to block the actual highlight flow.
            with mock.patch.object(word_picker, "HIGHLIGHT_LOG_PATH", Path(temporary)):
                word_picker.record_highlight_event("operation_error", error="bad path")

    def test_bridge_retry_trace_correlates_a_single_capture(self):
        client = word_picker.ObsidianBridgeClient.__new__(word_picker.ObsidianBridgeClient)
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "trace.log"
            with mock.patch.object(word_picker, "HIGHLIGHT_LOG_PATH", path), mock.patch.object(
                client, "_request", side_effect=[
                    word_picker.ObsidianBridgeTimeout("timed out"),
                    {"highlight_retry": True, "last_highlight_ms": 3},
                    {"file_path": "a.md", "highlight_id": "hl-1"},
                ],
            ):
                client.apply_highlight("capture-traced", "yellow")
            events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual([entry["event"] for entry in events], [
                "bridge_request", "bridge_error", "retry_supported",
                "bridge_request", "bridge_response",
            ])
            self.assertTrue(all(entry["capture_id"] == "capture-traced" for entry in events))
            self.assertEqual(events[-1]["highlight_id"], "hl-1")
            self.assertIn("elapsed_ms", events[1])
            self.assertNotIn("authorization", path.read_text(encoding="utf-8").lower())

    def test_unexpected_highlight_worker_error_is_reported_and_logged(self):
        app = word_picker.WordPickerApp.__new__(word_picker.WordPickerApp)
        app.events = mock.Mock()
        context = word_picker.ObsidianCaptureContext(
            vault_path="C:/vault", capture_id="capture-crash", file_path="a.md",
            file_name="a.md", selection="text",
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "trace.log"
            with mock.patch.object(word_picker, "HIGHLIGHT_LOG_PATH", path), mock.patch.object(
                word_picker, "ObsidianBridgeClient", side_effect=ValueError("unexpected")
            ):
                app._run_highlight(context, "yellow", "#e0ac00", "text")
            self.assertEqual(app.events.put.call_args.args[0][0], "highlight_error")
            self.assertIn("ValueError", app.events.put.call_args.args[0][2])
            self.assertIn("operation_unexpected_error", path.read_text(encoding="utf-8"))

    def test_question_title_defaults_to_level_two(self):
        self.assertEqual(
            dict(word_picker.QUESTION_TITLE_OPTIONS)[
                word_picker.DEFAULT_QUESTION_TITLE_LABEL
            ],
            2,
        )

    def test_code_language_recommendation_is_conservative(self):
        self.assertEqual(
            word_picker.recommend_code_language("# comment\ndef run():\n    pass"),
            "python",
        )
        self.assertEqual(
            word_picker.recommend_code_language("public class App {\n}"), "java"
        )
        self.assertEqual(
            word_picker.recommend_code_language("SELECT id FROM users;"), "sql"
        )
        self.assertEqual(
            word_picker.recommend_code_language("#include <stdio.h>\nprintf(\"ok\");"),
            "c",
        )
        self.assertEqual(
            word_picker.recommend_code_language("#include <vector>\nstd::vector<int> ids;"),
            "cpp",
        )
        self.assertEqual(
            word_picker.recommend_code_language('{"enabled": true}'), "json"
        )
        self.assertEqual(
            word_picker.recommend_code_language("# 普通 Markdown 标题"), ""
        )

    def test_note_normalization_preserves_spaces_tabs_and_blank_lines(self):
        raw = "\r\n    def run():\r\n\tif True:\r\n\t    print('a  b')  \r\n\r\n    return 1\r\n"
        expected = "    def run():\n\tif True:\n\t    print('a  b')  \n\n    return 1"
        self.assertEqual(word_picker.normalize_note_text(raw), expected)
        self.assertIn(expected, word_picker.markdown_code_block(
            word_picker.normalize_note_text(raw), "python"))
        self.assertEqual(word_picker.normalize_note_text(" \t\r\n"), "")
        self.assertEqual(word_picker.normalize_selected_text("    a\n\tb  c"), "a b c")

    def test_question_and_highlight_capture_preserve_selected_code_indentation(self):
        for mode in (word_picker.MODE_QUESTION,):
            with self.subTest(mode=mode):
                app = word_picker.WordPickerApp.__new__(word_picker.WordPickerApp)
                app.capture_mode = mode
                app.capture_revision = 7
                app.capture_paused = False
                app.capture = mock.Mock()
                app.capture.clipboard_sequence.return_value = 99
                app.capture.read_clipboard_text.return_value = "    if ready:\r\n\t    run('a  b')"
                app.config = mock.Mock(values={"minimum_text_length": 2})
                app._offer_capture = mock.Mock()
                app._finish_capture(7, (320, 240), 7)
                app._offer_capture.assert_called_once_with(
                    "    if ready:\n\t    run('a  b')", (320, 240), 7, None)

    def test_markdown_code_block_uses_a_safe_fence(self):
        block = word_picker.markdown_code_block(
            "# comment\nvalue = '```'", "python"
        )
        self.assertTrue(block.startswith("````python\n"))
        self.assertTrue(block.endswith("\n````"))
        self.assertIn("# comment", block)

    def test_shared_code_button_requires_a_selection(self):
        app = word_picker.WordPickerApp.__new__(word_picker.WordPickerApp)
        target = mock.Mock()
        target.winfo_exists.return_value = True
        target.index.side_effect = tk.TclError()
        app.question_code_target = target
        app._set_source_action_status = mock.Mock()

        result = app._insert_question_code(None)

        self.assertEqual(result, "break")
        target.clipboard_get.assert_not_called()
        target.insert.assert_not_called()
        app._set_source_action_status.assert_called_once_with(
            "请先用鼠标选择要转换为代码块的文字。"
        )

    def test_shared_code_button_converts_selected_text(self):
        app = word_picker.WordPickerApp.__new__(word_picker.WordPickerApp)
        target = mock.Mock()
        target.winfo_exists.return_value = True
        target.index.side_effect = ["2.0", "4.0"]
        target.get.side_effect = [
            "# comment\ndef run():\n    pass",
            "\n\n",
            "",
        ]
        app.question_code_target = target
        app._set_source_action_status = mock.Mock()

        result = app._insert_question_code("python")

        self.assertEqual(result, "break")
        target.delete.assert_called_once_with("2.0", "4.0")
        inserted = target.insert.call_args.args[1]
        self.assertTrue(inserted.startswith("```python\n"))
        self.assertIn("# comment", inserted)
        app._set_source_action_status.assert_called_once_with(
            "已作为 python 代码块插入。"
        )

    def test_image_target_tracks_the_last_clicked_editor(self):
        app = word_picker.WordPickerApp.__new__(word_picker.WordPickerApp)
        app.question_supplement_input = mock.Mock()
        app.question_selected_text_input = mock.Mock()
        app.question_supplement_input.index.return_value = "2.3"
        app.question_selected_text_input.index.return_value = "4.1"

        app._remember_question_image_target(
            mock.Mock(widget=app.question_supplement_input)
        )
        self.assertIs(app.question_image_target, app.question_supplement_input)
        self.assertEqual(app.question_image_insert_index, "2.3")

        app._remember_question_image_target(
            mock.Mock(widget=app.question_selected_text_input)
        )
        self.assertIs(app.question_image_target, app.question_selected_text_input)
        self.assertEqual(app.question_image_insert_index, "4.1")

    def test_new_selection_replaces_open_popup(self):
        app = word_picker.WordPickerApp.__new__(word_picker.WordPickerApp)
        app.monitoring = True
        app.capture_mode = word_picker.MODE_TRANSLATE
        app.capture_paused = True
        app.lookup_popup = mock.Mock()
        app.lookup_popup.winfo_exists.return_value = True
        app._close_popup = mock.Mock(side_effect=lambda: setattr(app, "capture_paused", False))
        app.last_pointer = (0, 0)
        app.capture = mock.Mock()
        app.capture.clipboard_sequence.return_value = 10
        app.root = mock.Mock()
        app._on_drag((320, 240))
        app._close_popup.assert_called_once_with()
        app.capture.send_copy.assert_not_called()
        app.capture.clipboard_sequence.assert_not_called()
        app.root.after.assert_not_called()
        self.assertTrue(app.pending_capture["deferred"])
        self.assertEqual(app.last_pointer, (320, 240))

    def test_question_popup_ignores_new_selection_until_closed(self):
        app = word_picker.WordPickerApp.__new__(word_picker.WordPickerApp)
        app.monitoring = True
        app.capture_mode = word_picker.MODE_QUESTION
        app.capture_paused = True
        app.lookup_popup = mock.Mock()
        app.lookup_popup.winfo_exists.return_value = True
        app._close_popup = mock.Mock()
        app.capture = mock.Mock()

        app._on_drag((320, 240))

        app._close_popup.assert_not_called()
        app.capture.send_copy.assert_not_called()

    def test_supplement_editor_delete_key_does_not_remove_selected_image(self):
        app = word_picker.WordPickerApp.__new__(word_picker.WordPickerApp)
        app.question_image_selected_index = 0
        app.lookup_popup = mock.Mock()
        app.lookup_popup.winfo_exists.return_value = True
        app.question_input = mock.Mock()
        app.question_supplement_input = mock.Mock()
        app.question_selected_text_input = mock.Mock()
        app.lookup_popup.focus_get.return_value = app.question_supplement_input
        app._remove_question_image = mock.Mock()

        result = app._remove_question_image_from_key()

        self.assertIsNone(result)
        app._remove_question_image.assert_not_called()

    def test_old_delayed_capture_is_ignored_after_new_selection(self):
        # 划词后改成轮询剪贴板，但「旧那一轮必须作废」这条不变：第一次划词排下的
        # 轮询在第二次划词之后不能再读剪贴板，更不能弹窗。
        app = word_picker.WordPickerApp.__new__(word_picker.WordPickerApp)
        app.monitoring = True
        app.capture_mode = word_picker.MODE_TRANSLATE
        app.capture_paused = True
        app.lookup_popup = mock.Mock()
        app.lookup_popup.winfo_exists.return_value = True
        app._close_popup = mock.Mock(side_effect=lambda: setattr(app, "capture_paused", False))
        app.capture = mock.Mock()
        app.capture.clipboard_sequence.return_value = 10   # 一直没变 → 一直轮询
        app.root = mock.Mock()
        callbacks = []
        app.root.after.side_effect = lambda _delay, callback: callbacks.append(callback)

        app._on_drag((100, 100))
        app._on_float_entry_click(100, 100)
        app._on_drag((200, 200))
        app._on_float_entry_click(200, 200)

        self.assertEqual(len(callbacks), 2, "每次点击记录后开始轮询，各一条")
        callbacks[0]()                       # 第一轮的那次轮询迟到了
        app.capture.read_clipboard_text.assert_not_called()

    def test_clipboard_poll_continues_as_soon_as_the_copy_lands(self):
        # 以前是干等固定 220 ms；现在复制一到就走，不用等满。
        app = word_picker.WordPickerApp.__new__(word_picker.WordPickerApp)
        app.capture_revision = 5
        app.capture = mock.Mock()
        app.capture.clipboard_sequence.side_effect = [10, 11]  # 第一次没变，第二次变了
        app.root = mock.Mock()
        callbacks = []
        app.root.after.side_effect = lambda _delay, callback: callbacks.append(callback)
        app._finish_capture = mock.Mock()

        app._poll_clipboard(10, (300, 200), 5, waited_ms=0)
        app._finish_capture.assert_not_called()      # 还没复制完，继续等
        self.assertEqual(len(callbacks), 1)
        self.assertEqual(
            app.root.after.call_args.args[0],
            word_picker.CLIPBOARD_POLL_INTERVAL_MS,
        )

        callbacks[0]()                                # 剪贴板换了
        app._finish_capture.assert_called_once_with(10, (300, 200), 5)

    def test_clipboard_poll_gives_up_at_the_timeout(self):
        # 复制很慢的程序：到上限仍然照旧走一次 _finish_capture，让里面那道
        # 「序号没变就返回」的校验去兜底——行为和改之前的固定等待完全一致。
        app = word_picker.WordPickerApp.__new__(word_picker.WordPickerApp)
        app.capture_revision = 5
        app.capture = mock.Mock()
        app.capture.clipboard_sequence.return_value = 10
        app.root = mock.Mock()
        app._finish_capture = mock.Mock()

        app._poll_clipboard(
            10, (300, 200), 5,
            waited_ms=word_picker.CLIPBOARD_POLL_TIMEOUT_MS,
        )

        app._finish_capture.assert_called_once_with(10, (300, 200), 5)
        app.root.after.assert_not_called()

    def test_paused_listener_without_popup_stays_paused(self):
        app = word_picker.WordPickerApp.__new__(word_picker.WordPickerApp)
        app.monitoring = True
        app.capture_paused = True
        app.lookup_popup = None
        app.capture = mock.Mock()
        app._on_drag((320, 240))
        app.capture.send_copy.assert_not_called()

    def test_question_document_writer_appends_markdown_heading_and_text(self):
        with tempfile.TemporaryDirectory() as temporary:
            document = Path(temporary) / "questions.md"
            word_picker.QuestionDocumentWriter.append(
                document, "Why does this happen?", "  First line  \nSecond line\n", 2
            )
            word_picker.QuestionDocumentWriter.append(document, "", "Another excerpt", 0)
            content = document.read_text(encoding="utf-8-sig")
            self.assertIn("## Why does this happen?\n\n  First line  \nSecond line", content)
            self.assertTrue(content.endswith("Another excerpt\n\n"))

    def test_question_document_writer_rejects_empty_selection(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(word_picker.DocumentError):
                word_picker.QuestionDocumentWriter.append(
                    Path(temporary) / "questions.md", "Question", "  \n", 1
                )

    def test_question_document_writer_copies_image_and_adds_relative_markdown(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            document = folder / "questions.md"
            source = folder / "source.png"
            source.write_bytes((word_picker.POPUP_CHARACTER_DIR / "frame_000.png").read_bytes())
            word_picker.QuestionDocumentWriter.append(
                document, "What is this?", "An excerpt", 1, image_path=source
            )
            content = document.read_text(encoding="utf-8-sig")
            self.assertIn("# What is this?", content)
            self.assertIn("An excerpt\n\n![图片](questions-images/", content)
            copied = list((folder / "questions-images").glob("*.png"))
            self.assertEqual(len(copied), 1)

    def test_question_document_writer_keeps_multiple_images_in_order(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            document = folder / "questions.md"
            source = folder / "source.png"
            source.write_bytes((word_picker.POPUP_CHARACTER_DIR / "frame_000.png").read_bytes())
            word_picker.QuestionDocumentWriter.append(
                document,
                "Question",
                "Excerpt",
                image_items=[(source, None), (source, None)],
            )
            content = document.read_text(encoding="utf-8-sig")
            self.assertEqual(content.count("![图片](questions-images/"), 2)
            copied = list((folder / "questions-images").glob("*.png"))
            self.assertEqual(len(copied), 2)

    def test_question_document_writer_preserves_interleaved_editor_content(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            document = folder / "questions.md"
            source = folder / "source.png"
            source.write_bytes((word_picker.POPUP_CHARACTER_DIR / "frame_000.png").read_bytes())
            word_picker.QuestionDocumentWriter.append(
                document,
                "Question",
                "Before",
                content_items=[
                    ("text", "Before\n"),
                    ("image", (source, None)),
                    ("text", "After"),
                ],
            )
            content = document.read_text(encoding="utf-8-sig")
            image_marker = content.index("![图片](questions-images/")
            self.assertLess(content.index("Before"), image_marker)
            self.assertLess(image_marker, content.index("After"))

    def test_compact_question_writer_links_to_ordered_detail_blocks(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            document = folder / "questions.md"
            with (
                mock.patch.object(word_picker.time, "time_ns", return_value=0xABC),
                mock.patch.object(word_picker.random, "getrandbits", return_value=1),
            ):
                word_picker.QuestionDocumentWriter.append_compact_question(
                    document,
                    "Short overview",
                    "Captured source body",
                    2,
                    supplement="Supplement body",
                    content_items=[("text", "Captured source body")],
                    include_index_source_link=True,
                    include_supplement_link=True,
                )

            detail = word_picker.question_detail_document_path(document)
            index_content = document.read_text(encoding="utf-8-sig")
            detail_content = detail.read_text(encoding="utf-8-sig")
            source_link = (
                f"[[./{detail.name}#^nh-source-abc-000001|问题/知识点来源]]"
            )
            supplement_link = (
                f"[[./{detail.name}#^nh-supplement-abc-000001|"
                "内容补充和对原文的进一步说明]]"
            )

            self.assertIn("## Short overview", index_content)
            self.assertIn(source_link, index_content)
            self.assertIn(supplement_link, index_content)
            self.assertNotIn("Captured source body", index_content)
            self.assertNotIn("Supplement body", index_content)
            self.assertLess(
                detail_content.index("Captured source body"),
                detail_content.index("Supplement body"),
            )
            self.assertIn("^nh-source-abc-000001", detail_content)
            self.assertIn("^nh-supplement-abc-000001", detail_content)

    def test_question_record_writer_creates_isolated_body_and_two_index_links(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            index = folder / "index.md"
            body_root = folder / "正文库"
            source = folder / "source.png"
            source.write_bytes((word_picker.POPUP_CHARACTER_DIR / "frame_000.png").read_bytes())
            body = word_picker.QuestionDocumentWriter.append_record(
                index,
                body_root,
                "BPE 与 WordPiece",
                "原文内容",
                supplement="补充内容",
                content_items=[("text", "原文内容"), ("image", (source, None))],
            )
            self.assertIsNotNone(body)
            body_path = Path(body)
            self.assertTrue(body_path.is_file())
            index_content = index.read_text(encoding="utf-8-sig")
            body_content = body_path.read_text(encoding="utf-8-sig")
            self.assertIn("|原文]]", index_content)
            self.assertIn("|补充说明]]", index_content)
            self.assertIn("## 原文", body_content)
            self.assertIn("## 补充说明", body_content)
            self.assertEqual(len(list(body_path.parent.glob("image-*.png"))), 1)

    def test_question_inline_image_is_in_record_directory_not_vault_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            index = folder / "高光之外的问题索引.md"
            body_root = folder / "正文库"
            class PastedImage:
                def save(self, destination, format):
                    self_format = format
                    assert self_format == "PNG"
                    Path(destination).write_bytes(b"pasted image bytes")
            result = word_picker.QuestionDocumentWriter.append_record(
                index, body_root, "问题", "原文",
                content_items=[("text", "原文"), ("image", (None, PastedImage()))],
                record_mode=word_picker.QUESTION_RECORD_MODE_INLINE,
            )
            self.assertIsNone(result)
            body = index.read_text(encoding="utf-8-sig")
            copied = list(body_root.glob("*/image-*.png"))
            self.assertEqual(len(copied), 1)
            relative = copied[0].relative_to(folder).as_posix()
            self.assertIn(f"![图片]({relative})", body)
            self.assertFalse(list(folder.glob("image-*.png")))
            self.assertFalse(list(body_root.glob("*/*.md")))

    def test_question_mixed_inline_image_uses_body_record_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            index = folder / "index.md"
            body_root = folder / "正文库"
            source = folder / "source.png"
            source.write_bytes((word_picker.POPUP_CHARACTER_DIR / "frame_000.png").read_bytes())
            body_path = word_picker.QuestionDocumentWriter.append_record(
                index, body_root, "问题", "原文",
                content_items=[("text", "原文"), ("image", (source, None))],
                supplement_items=[("text", "补充"), ("image", (source, None))],
                include_index_source_link=False, include_supplement_link=True,
            )
            self.assertIsNotNone(body_path)
            body_path = Path(body_path)
            images = list(body_path.parent.glob("image-*.png"))
            self.assertEqual(len(images), 2)
            index_text = index.read_text(encoding="utf-8-sig")
            body_text = body_path.read_text(encoding="utf-8-sig")
            self.assertTrue(any(f"![图片]({image.relative_to(folder).as_posix()})" in index_text for image in images))
            self.assertTrue(any(f"![图片]({image.name})" in body_text for image in images))
            self.assertFalse(list(folder.glob("image-*.png")))

    def test_highlight_paths_reject_outside_vault(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary) / "vault"
            vault.mkdir()
            inside = vault / "高光数据.jsonl"
            self.assertEqual(word_picker.highlight_vault_relative(str(inside), str(vault), ".jsonl"),
                             "高光数据.jsonl")
            with self.assertRaises(word_picker.ObsidianBridgeError):
                word_picker.highlight_vault_relative(str(Path(temporary) / "outside.jsonl"),
                                                      str(vault), ".jsonl")
            with self.assertRaises(word_picker.ObsidianBridgeError):
                word_picker.highlight_vault_relative(str(vault / "notes.md"), str(vault), ".jsonl")

    def test_inline_question_record_does_not_create_body_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            index = folder / "index.md"
            body_root = folder / "正文库"
            result = word_picker.QuestionDocumentWriter.append_record(
                index,
                body_root,
                "短记录",
                "原文",
                supplement="补充",
                record_mode=word_picker.QUESTION_RECORD_MODE_INLINE,
            )
            self.assertIsNone(result)
            content = index.read_text(encoding="utf-8-sig")
            self.assertIn("---- 原始文档", content)
            self.assertIn("---- 补充说明", content)
            self.assertFalse(body_root.exists())

    def test_compact_question_writer_can_hide_supplement_link_without_losing_text(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            document = folder / "questions.md"
            with (
                mock.patch.object(word_picker.time, "time_ns", return_value=0xDEF),
                mock.patch.object(word_picker.random, "getrandbits", return_value=2),
            ):
                word_picker.QuestionDocumentWriter.append_compact_question(
                    document,
                    "Overview",
                    "Source body",
                    supplement="Private supplement",
                    include_index_source_link=False,
                    include_supplement_link=False,
                )

            detail = word_picker.question_detail_document_path(document)
            index_content = document.read_text(encoding="utf-8-sig")
            self.assertIn("原始文档", index_content)
            self.assertIn("补充说明", index_content)
            self.assertNotIn("问题/知识点来源", index_content)
            self.assertNotIn("内容补充和对原文的进一步说明", index_content)
            self.assertIn("Source body", index_content)
            self.assertIn("Private supplement", index_content)
            self.assertLess(index_content.index("---- 原始文档"), index_content.index("Source body"))
            self.assertLess(index_content.index("---- 补充说明"), index_content.index("Private supplement"))
            self.assertFalse(detail.exists())

    def test_source_document_link_is_always_kept_in_source_note(self):
        app = word_picker.WordPickerApp.__new__(word_picker.WordPickerApp)
        item = {"type": "web", "url": "https://example.com/source"}

        linked = app._source_display_text(item)

        self.assertIn("[网页链接](https://example.com/source)", linked)

    def test_source_button_cancels_existing_source_annotation(self):
        app = word_picker.WordPickerApp.__new__(word_picker.WordPickerApp)
        app.question_source_button = mock.Mock()
        app.question_source_button.winfo_exists.return_value = True
        app.question_sources = [{"type": "web"}]
        app._remove_question_sources = mock.Mock()

        app._on_source_button()

        app._remove_question_sources.assert_called_once_with()

    def test_source_library_keeps_snapshot_after_local_file_move(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            library = word_picker.SourceLibrary(folder / "sources.json")
            original = folder / "lesson.pdf"
            moved = folder / "archive" / "lesson.pdf"
            original.write_bytes(b"lesson")
            record_id = library.register(original)
            moved.parent.mkdir()
            original.rename(moved)
            snapshot = library.resolve(record_id)
            self.assertIsNotNone(snapshot)
            self.assertEqual(Path(snapshot).read_bytes(), b"lesson")

    def test_source_library_snapshot_survives_source_deletion(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            library = word_picker.SourceLibrary(folder / "sources.json")
            original = folder / "lesson.pdf"
            original.write_bytes(b"lesson")
            record_id = library.register(original)
            original.unlink()
            snapshot = library.resolve(record_id)
            self.assertIsNotNone(snapshot)
            self.assertEqual(Path(snapshot).read_bytes(), b"lesson")

    def test_source_library_recreates_missing_vault_snapshot(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            vault = folder / "vault"
            vault.mkdir()
            library = word_picker.SourceLibrary(folder / "sources.json")
            original = folder / "lesson.pdf"
            original.write_bytes(b"lesson")

            record_id = library.register(original, vault)
            first_snapshot = Path(library.resolve(record_id, vault))
            first_snapshot.unlink()

            library.register(original, vault)
            rebuilt_snapshot = Path(library.resolve(record_id, vault))
            link = library.markdown_link(record_id, vault / "questions.md")

            self.assertTrue(rebuilt_snapshot.is_file())
            self.assertEqual(rebuilt_snapshot.read_bytes(), b"lesson")
            self.assertIn(rebuilt_snapshot.relative_to(vault).as_posix(), link)

    def test_source_library_creates_obsidian_friendly_relative_link(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            vault = folder / "vault"
            vault.mkdir()
            library = word_picker.SourceLibrary(folder / "sources.json")
            original = folder / "lesson.pdf"
            document = vault / "questions.md"
            original.write_bytes(b"lesson")
            record_id = library.register(original, vault)
            link = library.markdown_link(record_id, document)
            self.assertIn("[来源：lesson.pdf]", link)
            self.assertIn("NotingHelper-Sources/", link)
            self.assertNotIn("obsidian://", link)
            self.assertTrue(Path(library.resolve(record_id)).is_file())

    def test_source_library_converts_docx_snapshot_and_links_pdf(self):
        class FakeConverter:
            def __init__(self):
                self.calls = 0

            def convert(self, source_path, output_path):
                self.calls += 1
                Path(output_path).write_bytes(b"%PDF-1.7\nconverted pdf")
                return "test-converter"

            @staticmethod
            def is_valid_pdf(path):
                path = Path(path)
                return path.is_file() and path.read_bytes().startswith(b"%PDF-")

        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            vault = folder / "vault"
            vault.mkdir()
            converter = FakeConverter()
            library = word_picker.SourceLibrary(
                folder / "sources.json", docx_converter=converter
            )
            original = folder / "lesson.docx"
            original.write_bytes(b"docx source")

            record_id = library.register(original, vault)
            link_target = Path(library.resolve_link_target(record_id, vault))
            link = library.markdown_link(record_id, vault / "questions.md")

            self.assertIsNone(library.resolve(record_id, vault))
            self.assertFalse(
                any(
                    (vault / word_picker.SOURCE_LIBRARY_DIR_NAME).rglob("*.docx")
                )
            )
            self.assertEqual(link_target.suffix, ".pdf")
            self.assertEqual(link_target.read_bytes(), b"%PDF-1.7\nconverted pdf")
            self.assertIn("lesson.pdf（由 DOCX 转换）", link)
            self.assertIn(".pdf", link)
            self.assertNotIn(".docx>", link)

            link_target.unlink()
            library.register(original, vault)
            rebuilt_target = Path(library.resolve_link_target(record_id, vault))
            self.assertTrue(rebuilt_target.is_file())
            self.assertEqual(converter.calls, 2)

    def test_source_library_keeps_docx_link_when_conversion_is_unavailable(self):
        converter = mock.Mock()
        converter.convert.return_value = None
        converter.last_error = "没有可用转换引擎"
        converter.is_valid_pdf.return_value = False
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            vault = folder / "vault"
            vault.mkdir()
            library = word_picker.SourceLibrary(
                folder / "sources.json", docx_converter=converter
            )
            original = folder / "lesson.docx"
            original.write_bytes(b"docx source")

            record_id = library.register(original, vault)
            link = library.markdown_link(record_id, vault / "questions.md")

            self.assertIn("lesson.docx", link)
            self.assertTrue(Path(library.resolve(record_id, vault)).is_file())
            self.assertEqual(
                library.conversion_error(record_id), "没有可用转换引擎"
            )

    def test_external_source_link_explicitly_has_no_positioning(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            vault = folder / "vault"
            vault.mkdir()
            library = word_picker.SourceLibrary(folder / "sources.json")
            original = folder / "lesson.docx"
            document = vault / "questions.md"
            original.write_bytes(b"lesson")
            record_id = library.register(original, vault)
            app = word_picker.WordPickerApp.__new__(word_picker.WordPickerApp)
            app.source_library = library
            app.config = mock.Mock(values={"question_output_path": str(document)})

            rendered = app._source_display_text(
                {"type": "external", "record_id": record_id}
            )

            self.assertIn("[来源：lesson.docx]", rendered)
            self.assertIn(word_picker.EXTERNAL_SOURCE_NOTICE, rendered)
            self.assertNotIn("#page=", rendered)
            self.assertNotIn("<!--", rendered)

    def test_source_click_waits_for_detection_and_then_continues(self):
        app = word_picker.WordPickerApp.__new__(word_picker.WordPickerApp)
        app.source_detection_pending = True
        app.source_detection_revision = 7
        app.source_mark_requested_revision = -1
        app._set_source_action_status = mock.Mock()

        app._mark_detected_source()

        self.assertEqual(app.source_mark_requested_revision, 7)
        app._set_source_action_status.assert_called_once_with(
            "正在识别当前文档，完成后会自动加入来源…", busy=True
        )

        context = word_picker.DetectedSourceContext(
            application="WPS", path="C:/documents/lesson.docx"
        )
        app._mark_detected_source = mock.Mock()
        app._handle_source_context_ready(7, context)

        self.assertFalse(app.source_detection_pending)
        self.assertEqual(app.detected_source_context, context)
        self.assertEqual(app.source_mark_requested_revision, -1)
        app._mark_detected_source.assert_called_once_with()

    def test_question_record_defaults_include_obsidian_source_link(self):
        self.assertEqual(
            word_picker.AppConfig.DEFAULTS["question_source_link_mode"],
            word_picker.QUESTION_RECORD_MODE_INLINE,
        )
        self.assertEqual(
            word_picker.AppConfig.DEFAULTS["question_supplement_link_mode"],
            word_picker.QUESTION_RECORD_MODE_INLINE,
        )
        self.assertEqual(
            word_picker.AppConfig.DEFAULTS["question_title_level"], 2
        )
        self.assertTrue(word_picker.AppConfig.DEFAULTS["question_obsidian_source_link"])

    def test_question_page_loads_saved_default_settings(self):
        app = word_picker.WordPickerApp.__new__(word_picker.WordPickerApp)
        app.config = mock.Mock(
            values={
                "question_source_link_mode": word_picker.QUESTION_RECORD_MODE_INDEPENDENT,
                "question_supplement_link_mode": word_picker.QUESTION_RECORD_MODE_INLINE,
                "question_title_level": 4,
                "question_obsidian_source_link": False,
            }
        )
        app.question_source_default_var = mock.Mock()
        app.question_supplement_default_var = mock.Mock()
        app.question_title_level_default_var = mock.Mock()
        app.question_obsidian_source_link_var = mock.Mock()

        app._load_question_default_settings()

        app.question_source_default_var.set.assert_called_once_with("独立正文 + 索引链接")
        app.question_supplement_default_var.set.assert_called_once_with("直接写入当前索引")
        app.question_title_level_default_var.set.assert_called_once_with("四级标题")
        app.question_obsidian_source_link_var.set.assert_called_once_with(False)

    def test_obsidian_source_is_auto_marked_but_external_source_is_not(self):
        app = word_picker.WordPickerApp.__new__(word_picker.WordPickerApp)
        app.source_detection_pending = False
        app.source_detection_revision = 3
        app.source_mark_requested_revision = -1
        app.source_auto_mark_requested_revision = -1
        app.question_sources = []
        app.config = mock.Mock(values={"question_obsidian_source_link": True})
        app.question_source_index_link_var = mock.Mock()
        app.question_selected_text_input = mock.Mock()
        app.question_selected_text_input.winfo_exists.return_value = True
        app._set_source_action_status = mock.Mock()
        app._mark_detected_source = mock.Mock()
        obsidian_context = word_picker.ObsidianFileContext(
            vault_path="C:/vault",
            file_path="docs/source.pdf",
            file_name="source.pdf",
            extension="pdf",
            page_number=4,
        )

        app._handle_source_context_ready(3, obsidian_context)
        app._mark_detected_source.assert_called_once_with()
        app.question_source_index_link_var.set.assert_not_called()

        app._mark_detected_source.reset_mock()
        app._handle_source_context_ready(
            3,
            word_picker.DetectedSourceContext(
                application="Word", path="C:/documents/source.docx"
            ),
        )
        app._mark_detected_source.assert_not_called()

    def test_obsidian_source_waits_for_question_editor(self):
        app = word_picker.WordPickerApp.__new__(word_picker.WordPickerApp)
        app.source_detection_pending = False
        app.source_detection_revision = 4
        app.source_mark_requested_revision = -1
        app.source_auto_mark_requested_revision = -1
        app.question_sources = []
        app.config = mock.Mock(values={"question_obsidian_source_link": True})
        app.question_selected_text_input = None
        app._set_source_action_status = mock.Mock()
        app._mark_detected_source = mock.Mock()
        context = word_picker.ObsidianFileContext(
            vault_path="C:/vault",
            file_path="docs/source.md",
            file_name="source.md",
            extension="md",
            page_number=None,
        )

        app._handle_source_context_ready(4, context)

        self.assertEqual(app.source_auto_mark_requested_revision, 4)
        app.question_selected_text_input = mock.Mock()
        app.question_selected_text_input.winfo_exists.return_value = True
        app._apply_pending_obsidian_source()

        self.assertEqual(app.source_auto_mark_requested_revision, -1)
        app._mark_detected_source.assert_called_once_with()

    def test_source_library_deduplicates_identical_content(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            vault = folder / "vault"
            vault.mkdir()
            library = word_picker.SourceLibrary(folder / "sources.json")
            first = folder / "first.pdf"
            second = folder / "second.pdf"
            first.write_bytes(b"same lesson")
            second.write_bytes(b"same lesson")
            first_id = library.register(first, vault)
            first_snapshot = library.resolve(first_id)
            second_id = library.register(second, vault)
            self.assertEqual(first_id, second_id)
            self.assertEqual(first_snapshot, library.resolve(second_id))
            self.assertEqual(
                len(list((vault / word_picker.SOURCE_LIBRARY_DIR_NAME).rglob("*.pdf"))),
                1,
            )

    def test_source_library_keeps_one_snapshot_in_each_vault(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            first_vault = folder / "first-vault"
            second_vault = folder / "second-vault"
            first_vault.mkdir()
            second_vault.mkdir()
            library = word_picker.SourceLibrary(folder / "sources.json")
            original = folder / "lesson.docx"
            original.write_bytes(b"lesson")

            record_id = library.register(original, first_vault)
            library.register(original, second_vault)

            first_link = library.markdown_link(
                record_id, first_vault / "questions.md"
            )
            second_link = library.markdown_link(
                record_id, second_vault / "questions.md"
            )
            self.assertIn("NotingHelper-Sources/", first_link)
            self.assertIn("NotingHelper-Sources/", second_link)
            self.assertNotIn("file:///", first_link)
            self.assertNotIn("file:///", second_link)
            window = word_picker.ForegroundWindowContext(
                handle=1,
                process_id=2,
                process_name="wps.exe",
                executable="wps.exe",
                title="lesson.docx - WPS Office",
            )
            second_context = library.context_from_window(window, second_vault)
            self.assertIsNotNone(second_context)
            self.assertTrue(Path(second_context.path).is_relative_to(second_vault))
            self.assertEqual(
                len(
                    list(
                        (first_vault / word_picker.SOURCE_LIBRARY_DIR_NAME).rglob(
                            "*.docx"
                        )
                    )
                ),
                1,
            )
            self.assertEqual(
                len(
                    list(
                        (second_vault / word_picker.SOURCE_LIBRARY_DIR_NAME).rglob(
                            "*.docx"
                        )
                    )
                ),
                1,
            )

    def test_source_library_matches_snapshot_to_foreground_title(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            vault = folder / "vault"
            vault.mkdir()
            library = word_picker.SourceLibrary(folder / "sources.json")
            source = folder / "lesson.pdf"
            source.write_bytes(b"lesson")
            library.register(source, vault)
            window = word_picker.ForegroundWindowContext(
                handle=1,
                process_id=2,
                process_name="wps.exe",
                executable="wps.exe",
                title="lesson.pdf - WPS Office",
            )

            context = library.context_from_window(window)

            self.assertIsNotNone(context)
            self.assertEqual(Path(context.path).name, "lesson.pdf")
            self.assertEqual(context.window_title, window.title)

    def test_source_library_matches_converted_docx_without_reimporting_pdf(self):
        class FakeConverter:
            @staticmethod
            def convert(source_path, output_path):
                Path(output_path).write_bytes(b"%PDF-1.7\nconverted")
                return "test-converter"

            @staticmethod
            def is_valid_pdf(path):
                path = Path(path)
                return path.is_file() and path.read_bytes().startswith(b"%PDF-")

        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            vault = folder / "vault"
            vault.mkdir()
            library = word_picker.SourceLibrary(
                folder / "sources.json", docx_converter=FakeConverter()
            )
            source = folder / "lesson.docx"
            source.write_bytes(b"docx source")
            record_id = library.register(source, vault)
            window = word_picker.ForegroundWindowContext(
                handle=1,
                process_id=2,
                process_name="wps.exe",
                executable="wps.exe",
                title="lesson.docx - WPS Office",
            )

            context = library.context_from_window(window, vault)

            self.assertIsNotNone(context)
            self.assertEqual(context.record_id, record_id)
            self.assertEqual(Path(context.path).suffix, ".pdf")

    def test_obsidian_source_uses_native_block_link(self):
        link = word_picker.obsidian_source_link(
            "课程/第一章.md", "第一章.md", "nh-a82f19"
        )
        self.assertEqual(
            link, "[[课程/第一章.md#^nh-a82f19|来源：第一章.md]]"
        )

    def test_obsidian_non_markdown_source_uses_vault_file_link(self):
        link = word_picker.obsidian_file_source_link(
            "资料/课程讲义.pdf", "课程讲义.pdf", 12
        )
        self.assertEqual(link, "[[资料/课程讲义.pdf#page=12|来源：课程讲义.pdf]]")
        self.assertNotIn("#^", link)
        app = word_picker.WordPickerApp.__new__(word_picker.WordPickerApp)
        rendered = app._source_display_text(
            {
                "type": "obsidian_file",
                "link": link,
                "extension": "pdf",
                "page_number": 12,
            }
        )
        self.assertIn(link, rendered)
        self.assertIn("定位到第 12 页", rendered)

    def test_obsidian_vault_is_inferred_from_question_document(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            vault = folder / "vault"
            notes = vault / "课程"
            (vault / ".obsidian").mkdir(parents=True)
            notes.mkdir()
            self.assertEqual(
                word_picker.find_obsidian_vault(notes / "问题.md"), vault
            )

    def test_obsidian_plugin_installer_preserves_runtime_settings(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            vault = folder / "vault"
            (vault / ".obsidian").mkdir(parents=True)
            destination = word_picker.ObsidianBridgeClient.install(vault)
            settings = destination / "data.json"
            settings.write_text('{"token":"keep-me","port":27124}', encoding="utf-8")

            word_picker.ObsidianBridgeClient.install(vault)

            self.assertTrue((destination / "manifest.json").is_file())
            self.assertTrue((destination / "main.js").is_file())
            self.assertTrue((destination / "styles.css").is_file())
            self.assertEqual(
                json.loads(settings.read_text(encoding="utf-8"))["token"], "keep-me"
            )

    def test_obsidian_plugin_versions_are_read_from_both_sides(self):
        # 「安装/更新插件」只做文件复制：源没变时点多少次都看不出任何变化，
        # 所以提示语必须能报出「仓库里原来哪一版、源现在哪一版」。
        source_version = word_picker.ObsidianBridgeClient.source_plugin_version()
        self.assertRegex(source_version, r"^\d+\.\d+\.\d+$")
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary) / "vault"
            (vault / ".obsidian").mkdir(parents=True)
            self.assertEqual(
                word_picker.ObsidianBridgeClient.installed_plugin_version(vault), ""
            )
            destination = word_picker.ObsidianBridgeClient.install(vault)
            self.assertEqual(
                word_picker.ObsidianBridgeClient.installed_plugin_version(vault),
                source_version,
            )
            # 读的必须是仓库里那份，不是源目录那份。
            (destination / "manifest.json").write_text(
                '{"version":"2.0.0"}', encoding="utf-8"
            )
            self.assertEqual(
                word_picker.ObsidianBridgeClient.installed_plugin_version(vault),
                "2.0.0",
            )
            (destination / "manifest.json").write_text("{ 坏文件", encoding="utf-8")
            self.assertEqual(
                word_picker.ObsidianBridgeClient.installed_plugin_version(vault), ""
            )

    def test_obsidian_bridge_client_captures_selection_over_loopback(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary) / "vault"
            plugin_dir = (
                vault / ".obsidian" / "plugins" / word_picker.OBSIDIAN_PLUGIN_ID
            )
            plugin_dir.mkdir(parents=True)
            (plugin_dir / "data.json").write_text(
                json.dumps({"token": "secret-token", "port": 27124}),
                encoding="utf-8",
            )
            response = mock.MagicMock()
            response.__enter__.return_value.read.return_value = json.dumps(
                {
                    "capture_id": "capture-1",
                    "file_path": "课程/第一章.md",
                    "file_name": "第一章.md",
                    "selection": "被划选的文本",
                }
            ).encode("utf-8")

            with mock.patch.object(
                word_picker.urllib.request, "urlopen", return_value=response
            ) as urlopen:
                context = word_picker.ObsidianBridgeClient(vault).capture()

            request = urlopen.call_args.args[0]
            self.assertEqual(request.full_url, "http://127.0.0.1:27124/capture")
            self.assertEqual(request.get_header("Authorization"), "Bearer secret-token")
            self.assertEqual(context.capture_id, "capture-1")
            self.assertEqual(context.file_path, "课程/第一章.md")

    def test_highlight_timeout_retries_only_when_running_plugin_supports_it(self):
        client = word_picker.ObsidianBridgeClient.__new__(word_picker.ObsidianBridgeClient)
        saved = {"file_path": "讲义.md", "highlight_id": "hl-1"}
        with mock.patch.object(client, "_request", side_effect=[
            word_picker.ObsidianBridgeTimeout("timed out"),
            {"highlight_retry": True}, saved,
        ]) as request:
            self.assertEqual(client.apply_highlight("capture-1", "yellow"), saved)
        self.assertEqual(request.call_count, 3)
        self.assertEqual(request.call_args_list[0].args[0], "/highlight")
        self.assertEqual(request.call_args_list[0].kwargs["timeout"], 5.0)
        self.assertEqual(request.call_args_list[1].args[0], "/health")
        self.assertEqual(request.call_args_list[2].args[0], "/highlight")
        self.assertEqual(request.call_args_list[0].args[1], request.call_args_list[2].args[1])
        with mock.patch.object(client, "_request", side_effect=[
            word_picker.ObsidianBridgeTransportError("connection reset"),
            {"highlight_retry": True}, saved,
        ]) as request:
            self.assertEqual(client.apply_highlight("capture-1", "yellow"), saved)
            self.assertEqual(request.call_count, 3)

        # 旧插件没有幂等保证；超时后不能不问能力就重复 POST。
        with mock.patch.object(client, "_request", side_effect=[
            word_picker.ObsidianBridgeTimeout("timed out"),
            {"highlight_retry": False},
        ]) as request:
            with self.assertRaisesRegex(word_picker.ObsidianBridgeError, "结果尚未确认"):
                client.apply_highlight("capture-2", "green")
            self.assertEqual(request.call_count, 2)

    def test_bridge_timeout_is_not_misreported_as_plugin_disconnected(self):
        client = word_picker.ObsidianBridgeClient.__new__(word_picker.ObsidianBridgeClient)
        client.port = 27124
        client.token = "test-token"
        with mock.patch.object(word_picker.urllib.request, "urlopen",
                               side_effect=TimeoutError("timed out")):
            with self.assertRaises(word_picker.ObsidianBridgeTimeout) as raised:
                client._request("/health", timeout=0.5)
        self.assertIn("超过 0.5 秒", str(raised.exception))
        self.assertNotIn("尚未启用", str(raised.exception))

    def test_obsidian_bridge_client_captures_pdf_as_vault_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary) / "vault"
            plugin_dir = (
                vault / ".obsidian" / "plugins" / word_picker.OBSIDIAN_PLUGIN_ID
            )
            plugin_dir.mkdir(parents=True)
            (plugin_dir / "data.json").write_text(
                json.dumps({"token": "secret-token", "port": 27124}),
                encoding="utf-8",
            )
            response = mock.MagicMock()
            response.__enter__.return_value.read.return_value = json.dumps(
                {
                    "kind": "obsidian-file",
                    "file_path": "资料/课程讲义.pdf",
                    "file_name": "课程讲义.pdf",
                    "extension": "pdf",
                    "page_number": 12,
                }
            ).encode("utf-8")

            with mock.patch.object(
                word_picker.urllib.request, "urlopen", return_value=response
            ):
                context = word_picker.ObsidianBridgeClient(vault).capture()

            self.assertIsInstance(context, word_picker.ObsidianFileContext)
            self.assertEqual(context.file_path, "资料/课程讲义.pdf")
            self.assertEqual(context.extension, "pdf")
            self.assertEqual(context.page_number, 12)

    def test_question_document_writer_adds_source_block_in_order(self):
        with tempfile.TemporaryDirectory() as temporary:
            document = Path(temporary) / "questions.md"
            word_picker.QuestionDocumentWriter.append(
                document,
                "Question",
                "Excerpt",
                content_items=[
                    ("text", "Excerpt"),
                    ("source", "标注来源\nhttps://example.com/article"),
                ],
            )
            content = document.read_text(encoding="utf-8-sig")
            self.assertIn(
                "Excerpt\n\n标注来源\nhttps://example.com/article", content
            )

    def test_popup_character_assets_are_ready(self):
        asset_dir = word_picker.POPUP_CHARACTER_DIR
        manifest = json.loads((asset_dir / "manifest.json").read_text(encoding="utf-8"))
        frames = sorted(asset_dir.glob("frame_*.png"))
        self.assertTrue(manifest["transparent"])
        self.assertEqual(manifest["frame_count"], 90)
        self.assertEqual(len(frames), manifest["frame_count"])
        self.assertEqual(manifest["frame_delay_ms"], 33)
        self.assertTrue(all(path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n") for path in frames))
        self.assertTrue(word_picker.popup_character_available())

        walking_dir = word_picker.POPUP_WALKING_CHARACTER_DIR
        walking_manifest = json.loads(
            (walking_dir / "manifest.json").read_text(encoding="utf-8")
        )
        walking_frames = sorted(walking_dir.glob("frame_*.png"))
        self.assertTrue(walking_manifest["transparent"])
        self.assertEqual(walking_manifest["repeat_mode"], "continuous_loop")
        self.assertEqual(walking_manifest["replay_delay_ms"], 0)
        self.assertEqual(walking_manifest["source_frame_start"], 89)
        self.assertEqual(walking_manifest["source_frame_match"], 141)
        self.assertEqual(walking_manifest["loop_selection"], "explicit")
        self.assertEqual(walking_manifest["frame_count"], 52)
        self.assertEqual(len(walking_frames), walking_manifest["frame_count"])
        self.assertTrue(word_picker.popup_character_available(walking_dir))

    def test_character_bake_is_spread_instead_of_blocking_the_open(self):
        """开弹窗不能再把 90 帧一次性烘完——那正是每次开窗卡一秒的根因。

        回归点很具体：CharacterActor(...) 返回时只该烘好第 0 帧，其余走 after
        分摊。原来是同步烘完全部，而弹窗的 Toplevel 建得比角色早，所以用户看到
        的是「窗口先出来，然后整个界面冻结一秒」。
        """
        if word_picker.CharacterActor is None:
            self.skipTest("角色层不可用（缺 Pillow）")
        try:
            root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(str(exc))
        root.withdraw()
        host = tk.Frame(root)
        host.pack()
        try:
            actor = word_picker.CharacterActor(
                host, word_picker.POPUP_CHARACTER_DIR, "#1e1e1e",
                scale=1.0, display_width=word_picker.POPUP_CHARACTER_DISPLAY_WIDTH,
            )
            baked, total = actor.bake_progress()
            self.assertGreater(total, 1, "素材没读出来，这条测不到东西")
            self.assertEqual(baked, 1, "构造时同步烘了不止第 0 帧，开窗又会被冻住")
            self.assertTrue(actor.canvas.winfo_exists())

            # 分摊出去的那部分要真能烘完，不然动画永远停在第一帧
            deadline = time.monotonic() + 20
            while actor.bake_progress()[0] < total and time.monotonic() < deadline:
                time.sleep(0.006)
                root.update()
            self.assertEqual(actor.bake_progress(), (total, total))

            # 烘完之后就进了缓存：同一份素材再开一次窗是瞬时的
            again = word_picker.CharacterActor(
                host, word_picker.POPUP_CHARACTER_DIR, "#1e1e1e",
                scale=1.0, display_width=word_picker.POPUP_CHARACTER_DISPLAY_WIDTH,
            )
            self.assertEqual(again.bake_progress(), (total, total),
                             "第二次开窗没命中缓存，等于又要重烘一遍")
        finally:
            root.destroy()

    def test_character_tick_waits_for_frames_still_baking(self):
        """帧还没烘完时，_tick 不能越界、不能跳帧、也不能把动画当成已经放完。

        三件事原来都挂在 len(self.frames) 上；那个值在烘焙收尾前是中间值，于是
        动画会提前走进「放完了」的分支（暂停十秒才重播）。
        """
        if word_picker.CharacterActor is None:
            self.skipTest("角色层不可用（缺 Pillow）")
        actor = word_picker.CharacterActor.__new__(word_picker.CharacterActor)
        actor.frames = ["f0", "f1", "f2"]
        actor.frame_delay_ms = 33
        actor.replay_delay_ms = 10_000
        actor.repeat_mode = "pause_then_replay"
        actor._after_id = None
        actor._image_item = 1
        shown: list = []
        scheduled: list = []

        class Canvas:
            def itemconfigure(self, item, image=None):
                shown.append(image)

            def winfo_exists(self):
                return True

            def after(self, ms, callback):
                scheduled.append((ms, callback))
                return "timer"

        actor.canvas = Canvas()

        # ① 要显示的那一帧还没烘出来 → 原地等一轮，不显示也不前进（不能跳帧）
        actor._total_frames = 90
        actor._index = 5
        actor._tick()
        self.assertEqual(shown, [], "取到了还没烘出来的帧")
        self.assertEqual(actor._index, 5, "跳过了还没烘出来的帧")
        self.assertEqual(scheduled, [(33, actor._tick)])

        # ② 这一帧烘出来了，但磁盘上还有很多帧 → 照常前进，不能当成放完
        actor._index = 2
        actor._tick()
        self.assertEqual(shown, ["f2"])
        self.assertEqual(actor._index, 3)
        self.assertEqual(scheduled[-1], (33, actor._tick))

        # ③ 磁盘上的最后一帧也烘出来了 → 这才是真的放完
        actor._total_frames = 3
        actor._index = 2
        actor._tick()
        self.assertEqual(actor._after_id, "timer")
        self.assertEqual(scheduled[-1], (10_000, actor.play))

    def test_character_destroy_keeps_the_cached_frame_list(self):
        """destroy() 不能清空缓存里的帧列表。

        命中的缓存和角色共用同一个 list 对象，所以这里必须重新绑定而不是
        frames.clear()——否则下一个命中缓存的角色就无帧可播。
        """
        if word_picker.CharacterActor is None:
            self.skipTest("角色层不可用（缺 Pillow）")
        shared = ["f0", "f1"]
        actor = word_picker.CharacterActor.__new__(word_picker.CharacterActor)
        actor.frames = shared
        actor._after_id = None
        actor._pump_after = None
        actor._bake_ctx = None
        actor.canvas = None
        actor.destroy()
        self.assertEqual(shared, ["f0", "f1"], "缓存里那份被 clear() 清掉了")
        self.assertEqual(actor.frames, [])

    def test_json_document_and_added_time_index(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            document = folder / "library.json"
            index = word_picker.VocabularyIndex(folder / "index.sqlite3")
            result = index.append_unique(document, [ITEM], "I selected a word.")
            self.assertEqual(result.added, ("select",))
            payload = json.loads(document.read_text(encoding="utf-8"))
            self.assertEqual(payload[0]["word"], "select")
            self.assertGreater(payload[0]["added_at"], 0)
            self.assertEqual(
                index.append_unique(document, [ITEM], "I selected a word.").duplicates,
                ("select",),
            )
            entries = index.list_vocabulary_entries()
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0].display_word, "select")
            self.assertGreater(entries[0].added_at, 0)

    def test_config_resolves_directory_and_filename(self):
        with tempfile.TemporaryDirectory() as temporary:
            config_path = Path(temporary) / "config.json"
            config_path.write_text(
                json.dumps({"output_path": str(Path(temporary) / "old.md")}),
                encoding="utf-8",
            )
            config = word_picker.AppConfig(config_path)
            self.assertEqual(
                word_picker.resolve_output_path(config.values), Path(temporary) / "old.md"
            )

    def test_config_resolves_question_document_legacy_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            question_path = Path(temporary) / "notes.md"
            config_path = Path(temporary) / "config.json"
            config_path.write_text(
                json.dumps({"question_output_path": str(question_path)}), encoding="utf-8"
            )
            config = word_picker.AppConfig(config_path)
            self.assertEqual(word_picker.resolve_question_output_path(config.values), question_path)

    def test_dir_template_resolves_relative_and_absolute_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary) / "vault"
            absolute = Path(temporary) / "outside"
            template = word_picker.normalized_question_dir_template(
                {
                    "index_dir": "NoteDataOfY/索引文档库",
                    "body_dir": "NoteDataOfY\\正文库",   # 反斜杠也认
                    "materials_dir": str(absolute),
                    "index_filename": "记录",           # 没带后缀
                }
            )
            defaults = word_picker.apply_question_dir_template(template, vault, create=True)
            self.assertEqual(defaults["question_output_dir"], str(vault / "NoteDataOfY/索引文档库"))
            self.assertEqual(defaults["question_body_dir"], str(vault / "NoteDataOfY/正文库"))
            self.assertEqual(defaults["question_materials_dir"], str(absolute))
            self.assertEqual(defaults["question_output_filename"], "记录.md")
            for key in ("question_output_dir", "question_body_dir", "question_materials_dir"):
                self.assertTrue(Path(defaults[key]).is_dir())

    def test_dir_template_falls_back_when_values_are_blank(self):
        template = word_picker.normalized_question_dir_template(
            {"index_dir": "", "body_dir": None, "materials_dir": "  ", "index_filename": ""}
        )
        self.assertEqual(template, word_picker.QUESTION_DIR_TEMPLATE)

    def test_selecting_question_history_updates_directory_and_filename(self):
        target = Path("C:/vault/课程二/问题.md")
        app = word_picker.WordPickerApp.__new__(word_picker.WordPickerApp)
        app.question_output_filename_var = mock.Mock()
        app.question_output_filename_var.get.return_value = "../课程二/问题.md"
        app.question_output_dir_var = mock.Mock()
        app.question_output_history_targets = {"../课程二/问题.md": target}

        app._on_question_document_history_selected()

        app.question_output_filename_var.set.assert_called_once_with("问题.md")
        app.question_output_dir_var.set.assert_called_once_with(str(target.parent))

    def test_question_vault_profiles_are_saved_separately(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            first_vault = folder / "first-vault"
            second_vault = folder / "second-vault"
            first_vault.mkdir()
            second_vault.mkdir()
            values = {}

            word_picker.record_question_vault_profile(
                values,
                first_vault,
                {
                    "question_output_dir": str(first_vault / "课程一"),
                    "question_output_filename": "问题一.md",
                    "question_body_dir": str(first_vault / "正文一"),
                    "question_materials_dir": str(first_vault / "资料一"),
                    "question_record_mode": word_picker.QUESTION_RECORD_MODE_INDEPENDENT,
                },
            )
            word_picker.record_question_vault_profile(
                values,
                second_vault,
                {
                    "question_output_dir": str(second_vault / "课程二"),
                    "question_output_filename": "问题二.md",
                    "question_body_dir": str(second_vault / "正文二"),
                    "question_materials_dir": str(second_vault / "资料二"),
                    "question_record_mode": word_picker.QUESTION_RECORD_MODE_INLINE,
                },
            )

            self.assertEqual(
                word_picker.question_vault_profile(values, first_vault)[
                    "question_output_filename"
                ],
                "问题一.md",
            )
            self.assertEqual(
                word_picker.question_vault_profile(values, second_vault)[
                    "question_output_filename"
                ],
                "问题二.md",
            )

    def test_switching_vault_restores_its_last_saved_question_profile(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary) / "vault"
            (vault / ".obsidian").mkdir(parents=True)
            document_dir = vault / "课程"
            values = {}
            word_picker.record_question_vault_profile(
                values,
                vault,
                {
                    "question_output_dir": str(document_dir),
                    "question_output_filename": "问题.md",
                    "question_body_dir": str(vault / "正文库"),
                    "question_materials_dir": str(vault / "文档资料库"),
                    "question_record_mode": word_picker.QUESTION_RECORD_MODE_INLINE,
                },
            )
            app = word_picker.WordPickerApp.__new__(word_picker.WordPickerApp)
            app.config = mock.Mock(values=values)
            app.obsidian_vault_var = mock.Mock()
            app.obsidian_vault_var.get.return_value = str(vault)
            app.loaded_question_vault_key = ""
            app.question_output_dir_var = mock.Mock()
            app.question_output_filename_var = mock.Mock()
            app.question_body_dir_var = mock.Mock()
            app.question_materials_dir_var = mock.Mock()
            app.question_record_mode_var = mock.Mock()
            app._refresh_question_document_history = mock.Mock()

            app._on_obsidian_vault_changed()

            app.question_output_dir_var.set.assert_called_once_with(str(document_dir))
            app.question_output_filename_var.set.assert_called_once_with("问题.md")
            app.question_body_dir_var.set.assert_called_once_with(str(vault / "正文库"))
            app.question_materials_dir_var.set.assert_called_once_with(
                str(vault / "文档资料库")
            )
            app.question_record_mode_var.set.assert_called_once_with(
                "直接写入当前索引"
            )

    def test_question_directory_dialog_starts_at_selected_vault(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary) / "vault"
            vault.mkdir()
            current = Path(temporary) / "current"
            current.mkdir()
            app = word_picker.WordPickerApp.__new__(word_picker.WordPickerApp)
            app.obsidian_vault_var = mock.Mock()
            app.obsidian_vault_var.get.return_value = str(vault)

            self.assertEqual(app._question_directory_dialog_start(current), vault)

    def test_switching_to_new_vault_applies_dir_template(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary) / "new-vault"
            (vault / ".obsidian").mkdir(parents=True)
            app = word_picker.WordPickerApp.__new__(word_picker.WordPickerApp)
            app.config = mock.Mock(values={})
            app.obsidian_vault_var = mock.Mock()
            app.obsidian_vault_var.get.return_value = str(vault)
            app.loaded_question_vault_key = ""
            app.question_output_dir_var = mock.Mock()
            app.question_output_filename_var = mock.Mock()
            app.question_body_dir_var = mock.Mock()
            app.question_materials_dir_var = mock.Mock()
            app.question_record_mode_var = None
            app._refresh_question_document_history = mock.Mock()

            app._on_obsidian_vault_changed()

            # 新仓库按默认模板落地：软件生成的东西都在 NoteDataOfY/ 下
            root = vault / word_picker.NOTE_DATA_DIR_NAME
            app.question_output_dir_var.set.assert_called_once_with(str(root / "索引文档库"))
            app.question_output_filename_var.set.assert_called_once_with(
                word_picker.QUESTION_DIR_TEMPLATE["index_filename"]
            )
            app.question_body_dir_var.set.assert_called_once_with(str(root / "正文库"))
            app.question_materials_dir_var.set.assert_called_once_with(str(root / "资料库"))
            # 写记录时索引取自 config.values、正文却取自 UI 变量，两边必须是同一份
            self.assertEqual(app.config.values["question_output_dir"], str(root / "索引文档库"))
            self.assertEqual(app.config.values["question_body_dir"], str(root / "正文库"))
            self.assertEqual(app.config.values["question_materials_dir"], str(root / "资料库"))
            self.assertEqual(
                app.config.values["question_output_path"],
                str(root / "索引文档库" / word_picker.QUESTION_DIR_TEMPLATE["index_filename"]),
            )

    def test_switching_to_known_vault_restores_profile_into_config(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary) / "known-vault"
            (vault / ".obsidian").mkdir(parents=True)
            app = word_picker.WordPickerApp.__new__(word_picker.WordPickerApp)
            app.config = mock.Mock(values={})
            word_picker.record_question_vault_profile(
                app.config.values,
                vault,
                {
                    "question_output_dir": str(vault / "大纲"),
                    "question_output_filename": "记录.md",
                    "question_body_dir": str(vault / "正文"),
                    "question_materials_dir": str(vault / "资料"),
                    "question_record_mode": word_picker.QUESTION_RECORD_MODE_INDEPENDENT,
                },
            )
            app.obsidian_vault_var = mock.Mock()
            app.obsidian_vault_var.get.return_value = str(vault)
            app.loaded_question_vault_key = ""
            app.question_output_dir_var = mock.Mock()
            app.question_output_filename_var = mock.Mock()
            app.question_body_dir_var = mock.Mock()
            app.question_materials_dir_var = mock.Mock()
            app.question_record_mode_var = None
            app._refresh_question_document_history = mock.Mock()

            app._on_obsidian_vault_changed()

            app.question_output_dir_var.set.assert_called_once_with(str(vault / "大纲"))
            app.question_body_dir_var.set.assert_called_once_with(str(vault / "正文"))
            self.assertEqual(app.config.values["question_output_dir"], str(vault / "大纲"))
            self.assertEqual(app.config.values["question_body_dir"], str(vault / "正文"))
            self.assertEqual(app.config.values["question_materials_dir"], str(vault / "资料"))

    def test_switching_to_directory_without_obsidian_still_applies_template(self):
        # 仓库可能在子目录里，用户很容易少选一层。缺 .obsidian 时不能一声不响，
        # 目录照常落地，并把「为什么定位还不能用」写在提示里。
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary) / "outer"
            folder.mkdir()
            app = word_picker.WordPickerApp.__new__(word_picker.WordPickerApp)
            app.config = mock.Mock(values={})
            app.obsidian_vault_var = mock.Mock()
            app.obsidian_vault_var.get.return_value = str(folder)
            app.loaded_question_vault_key = ""
            app.question_output_dir_var = mock.Mock()
            app.question_output_filename_var = mock.Mock()
            app.question_body_dir_var = mock.Mock()
            app.question_materials_dir_var = mock.Mock()
            app.question_record_mode_var = None
            app.obsidian_status_labels = [mock.Mock()]
            app._refresh_question_document_history = mock.Mock()

            app._on_obsidian_vault_changed()

            root = folder / word_picker.NOTE_DATA_DIR_NAME
            app.question_output_dir_var.set.assert_called_once_with(str(root / "索引文档库"))
            app.question_body_dir_var.set.assert_called_once_with(str(root / "正文库"))
            app.question_materials_dir_var.set.assert_called_once_with(str(root / "资料库"))
            app.obsidian_status_labels[0].configure.assert_called_once_with(
                text=word_picker.OBSIDIAN_STATUS_NEEDS_VAULT_NOTICE
            )

    def test_switching_vault_remembers_previous_profile(self):
        with tempfile.TemporaryDirectory() as temporary:
            old = Path(temporary) / "old-vault"
            (old / ".obsidian").mkdir(parents=True)
            new = Path(temporary) / "new-vault"
            (new / ".obsidian").mkdir(parents=True)
            app = word_picker.WordPickerApp.__new__(word_picker.WordPickerApp)
            app.config = mock.Mock(values={})
            app.loaded_question_vault_key = word_picker._normalized_path_key(old)
            app.obsidian_vault_var = mock.Mock()
            app.obsidian_vault_var.get.return_value = str(new)
            app.question_output_dir_var = mock.Mock()
            app.question_output_dir_var.get.return_value = str(old / "大纲")
            app.question_output_filename_var = mock.Mock()
            app.question_output_filename_var.get.return_value = "记录.md"
            app.question_body_dir_var = mock.Mock()
            app.question_body_dir_var.get.return_value = str(old / "正文")
            app.question_materials_dir_var = mock.Mock()
            app.question_materials_dir_var.get.return_value = str(old / "资料")
            app.question_record_mode_var = mock.Mock()
            app.question_record_mode_var.get.return_value = "创建独立正文并加入索引（推荐）"
            app.obsidian_status_label = mock.Mock()
            app._refresh_question_document_history = mock.Mock()

            app._on_obsidian_vault_changed()

            profile = word_picker.question_vault_profile(app.config.values, old)
            self.assertEqual(profile["question_output_dir"], str(old / "大纲"))
            self.assertEqual(profile["question_body_dir"], str(old / "正文"))
            self.assertEqual(
                profile["question_record_mode"], word_picker.QUESTION_RECORD_MODE_INDEPENDENT
            )
            app.config.save.assert_called_once()

    def test_previous_vault_profile_is_not_rewritten_when_unchanged(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary) / "vault"
            (vault / ".obsidian").mkdir(parents=True)
            app = word_picker.WordPickerApp.__new__(word_picker.WordPickerApp)
            app.config = mock.Mock(values={})
            app.loaded_question_vault_key = word_picker._normalized_path_key(vault)
            app.question_output_dir_var = mock.Mock()
            app.question_output_dir_var.get.return_value = str(vault / "大纲")
            app.question_output_filename_var = mock.Mock()
            app.question_output_filename_var.get.return_value = "记录.md"
            app.question_body_dir_var = mock.Mock()
            app.question_body_dir_var.get.return_value = str(vault / "正文")
            app.question_materials_dir_var = mock.Mock()
            app.question_materials_dir_var.get.return_value = str(vault / "资料")
            app.question_record_mode_var = mock.Mock()
            app.question_record_mode_var.get.return_value = "创建独立正文并加入索引（推荐）"

            app._remember_previous_question_vault()
            app.config.save.assert_called_once()

            # 同一个仓库没改动再切走一次：不该白写一次配置文件
            app.config.save.reset_mock()
            app._remember_previous_question_vault()
            app.config.save.assert_not_called()

    def test_previous_vault_is_not_remembered_from_placeholder_dirs(self):
        # 配置里只填了仓库、目录还是应用自己 data 目录时，那是占位值
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary) / "vault"
            (vault / ".obsidian").mkdir(parents=True)
            app = word_picker.WordPickerApp.__new__(word_picker.WordPickerApp)
            app.config = mock.Mock(values={})
            app.loaded_question_vault_key = word_picker._normalized_path_key(vault)
            app.question_output_dir_var = mock.Mock()
            app.question_output_dir_var.get.return_value = str(
                word_picker.DEFAULT_QUESTION_OUTPUT_PATH.parent
            )
            app.question_output_filename_var = mock.Mock()
            app.question_output_filename_var.get.return_value = "我的问题记录.md"
            app.question_body_dir_var = mock.Mock()
            app.question_body_dir_var.get.return_value = str(
                word_picker.DEFAULT_QUESTION_BODY_DIR
            )
            app.question_materials_dir_var = mock.Mock()
            app.question_materials_dir_var.get.return_value = str(
                word_picker.DEFAULT_QUESTION_MATERIALS_DIR
            )
            app.question_record_mode_var = None

            app._remember_previous_question_vault()

            self.assertEqual(app.config.values.get("question_vault_profiles"), None)
            app.config.save.assert_not_called()

    def test_dictionary_path_config_and_environment_override(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            configured = folder / "configured.csv"
            overridden = folder / "overridden.csv"
            config_path = folder / "config.json"
            config_path.write_text(
                json.dumps({"dictionary_path": str(configured)}), encoding="utf-8"
            )
            with mock.patch.dict(
                os.environ, {"WORD_PICKER_DICTIONARY_PATH": str(overridden)}
            ):
                config = word_picker.AppConfig(config_path)
            self.assertEqual(config.values["dictionary_path"], str(overridden))

    def test_dictionary_check_builds_and_refreshes_sqlite_index(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            source = folder / "dictionary.csv"
            index = folder / "dictionary.sqlite3"
            write_dictionary(source, ["select,si'lekt,,v. 选择,v.,,,,,,,,"])
            dictionary = word_picker.LocalDictionary(source, index)
            first = dictionary.check()
            self.assertTrue(first.rebuilt)
            self.assertEqual(first.entry_count, 1)
            self.assertFalse(dictionary.check().rebuilt)
            self.assertTrue(dictionary.lookup_many(["select"])["select"].is_complete)

            write_dictionary(
                source,
                [
                    "select,si'lekt,,v. 选择,v.,,,,,,,,",
                    "word,w3:d,,n. 单词,n.,,,,,,,,",
                ],
            )
            refreshed = dictionary.check()
            self.assertTrue(refreshed.rebuilt)
            self.assertEqual(refreshed.entry_count, 2)

    def test_hybrid_lookup_local_hit_does_not_call_api(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            source = folder / "dictionary.csv"
            write_dictionary(source, ["select,si'lekt,,v. 选择,v.,,,,,,,,"])
            local = word_picker.LocalDictionary(source, folder / "index.sqlite3")
            api = FakeApiService()
            service = word_picker.HybridVocabularyService(local, api)
            result = service.lookup(
                "I selected a word.",
                [{"word": "select", "seen_form": "selected"}],
            )
            self.assertEqual(api.calls, [])
            self.assertEqual(result[0]["meaning_zh"], "v. 选择")
            self.assertEqual(result[0]["example"], "I selected a word.")

    def test_hybrid_lookup_uses_api_for_partial_entry_and_preserves_order(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            source = folder / "dictionary.csv"
            write_dictionary(
                source,
                [
                    "api,,,n. 接口,n.,,,,,,,,",
                    "select,si'lekt,,v. 选择,v.,,,,,,,,",
                ],
            )
            local = word_picker.LocalDictionary(source, folder / "index.sqlite3")
            api = FakeApiService()
            service = word_picker.HybridVocabularyService(local, api)
            result = service.lookup(
                "The API selected it.",
                [
                    {"word": "api", "seen_form": "API"},
                    {"word": "select", "seen_form": "selected"},
                ],
            )
            self.assertEqual(api.calls, [["api"]])
            self.assertEqual([item["word"] for item in result], ["api", "select"])
            self.assertEqual(result[0]["meaning_zh"], "接口")
            self.assertEqual(result[1]["meaning_zh"], "v. 选择")

    def test_hybrid_lookup_without_api_keeps_local_results(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            source = folder / "dictionary.csv"
            write_dictionary(source, ["select,si'lekt,,v. 选择,v.,,,,,,,,"])
            local = word_picker.LocalDictionary(source, folder / "index.sqlite3")
            api = FakeApiService(api_key="")
            service = word_picker.HybridVocabularyService(local, api)
            result = service.lookup(
                "Select unknownproductname.",
                [
                    {"word": "select", "seen_form": "Select"},
                    {"word": "unknownproductname", "seen_form": "unknownproductname"},
                ],
            )
            self.assertEqual(api.calls, [])
            self.assertEqual([item["word"] for item in result], ["select"])

    def test_dictionary_exchange_drives_lemmatization(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            source = folder / "dictionary.csv"
            write_dictionary_records(
                source,
                [
                    {
                        "word": "encode",
                        "phonetic": "encode",
                        "translation": "v. 编码",
                        "exchange": "d:encoded/p:encoded/i:encoding/3:encodes",
                    },
                    {
                        "word": "encoding",
                        "phonetic": "encoding",
                        "translation": "n. 编码",
                        "exchange": "0:encode/1:i/i:encoding/s:encodings",
                    },
                    {
                        "word": "raise",
                        "phonetic": "raise",
                        "translation": "v. 提高",
                        "exchange": "d:raised/p:raised/i:raising/3:raises",
                    },
                    {
                        "word": "raised",
                        "phonetic": "raised",
                        "translation": "adj. 提高的",
                        "exchange": "0:raise/1:dp/d:raised/p:raised",
                    },
                    {
                        "word": "bed",
                        "phonetic": "bed",
                        "translation": "n. 床",
                        "exchange": "s:beds/d:bedded/p:bedded/i:bedding/3:beds",
                    },
                ],
            )
            dictionary = word_picker.LocalDictionary(source, folder / "index.sqlite3")
            lemmas = dictionary.lemmatize_many(
                ["encoding", "raised", "bed", "bedded"]
            )
            self.assertEqual(
                lemmas,
                {
                    "encoding": "encode",
                    "raised": "raise",
                    "bed": "bed",
                    "bedded": "bed",
                },
            )

            prepared = word_picker.prepare_vocabulary_words(
                "Encoding raised the bed.", {"raise"}, lemmas
            )
            self.assertEqual(
                [item["word"] for item in prepared], ["encode", "the", "bed"]
            )

            api = FakeApiService()
            service = word_picker.HybridVocabularyService(dictionary, api)
            local_only = word_picker.prepare_vocabulary_words(
                "Encoding raised bed.", lemma_map=lemmas
            )
            result = service.lookup("Encoding raised bed.", local_only)
            self.assertEqual(api.calls, [])
            self.assertEqual(
                [item["word"] for item in result], ["encode", "raise", "bed"]
            )

    def test_dictionary_path_recovers_after_checkout_move(self):
        with tempfile.TemporaryDirectory() as temporary:
            bundled = Path(temporary) / "ecdict.csv"
            bundled.write_text("word,phonetic,definition,translation,pos\nhello,,,你好,n.\n", encoding="utf-8")
            with mock.patch.object(word_picker, "DEFAULT_DICTIONARY_PATH", bundled):
                resolved = word_picker.resolve_dictionary_path(
                    {"dictionary_path": str(Path(temporary) / "old-project" / "ecdict.csv")}
                )
            self.assertEqual(resolved, bundled)

    def test_dictionary_path_keeps_existing_custom_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            custom = Path(temporary) / "custom.csv"
            custom.write_text("word,phonetic,definition,translation,pos\nhello,,,你好,n.\n", encoding="utf-8")
            with mock.patch.object(word_picker, "DEFAULT_DICTIONARY_PATH", Path(temporary) / "bundled.csv"):
                resolved = word_picker.resolve_dictionary_path({"dictionary_path": str(custom)})
            self.assertEqual(resolved, custom)
    def test_stats_window_is_available_in_a_bare_root(self):
        try:
            root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(str(exc))
        root.withdraw()
        app = word_picker.WordPickerApp.__new__(word_picker.WordPickerApp)
        app.root = root
        app.ui_scale = 1.0                     # _px() 要用；没跑 __init__，这里补上
        with tempfile.TemporaryDirectory() as temporary:
            app.word_index = word_picker.VocabularyIndex(Path(temporary) / "index.sqlite3")
            app.stats_window = None
            app.stats_tree = None
            app.stats_search_var = None
            app.stats_range_var = None
            app.stats_summary = None
            app._open_stats_window()
            root.update_idletasks()
            self.assertTrue(app.stats_window.winfo_exists())
            self.assertIsInstance(app.stats_tree, ttk.Treeview)
            app._close_stats_window()
        root.destroy()

    def test_default_palette_is_the_dark_obsidian_look(self):
        # 界面不做主题开关，暗色就是产品外观。有人把 default_palette 改回亮色、
        # 或误以为 Palette 的构造默认值就是产品外观时，这条会红。
        if word_picker.ui_theme is None:
            self.skipTest("front 主题层没加载")
        theme = word_picker.ui_theme
        palette = theme.default_palette()
        self.assertTrue(palette.dark)
        self.assertEqual(palette.bg_primary, theme.BASE_DARK["00"])
        self.assertNotEqual(palette.bg_primary, theme.BASE_LIGHT["00"])
        # 页面底色在暗色里等于卡片色：深灰页面会显得卡片是凹进去的
        self.assertEqual(palette.bg_page, theme.BASE_DARK["00"])
        self.assertNotEqual(palette.bg_page, palette.bg_secondary)
        # 亮色那套仍在，只是没人用：留着是为了能一行切回去
        self.assertFalse(theme.Palette().dark)
        self.assertEqual(theme.Palette().bg_page, theme.BASE_LIGHT["20"])

    def test_built_styles_use_the_dark_palette(self):
        if word_picker.ui_theme is None or word_picker.ui_icons is None:
            self.skipTest("front 主题层没加载")
        try:
            root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(str(exc))
        root.withdraw()
        app = word_picker.WordPickerApp.__new__(word_picker.WordPickerApp)
        app.root = root
        app.ui_scale = 1.0
        dark = word_picker.ui_theme.BASE_DARK
        try:
            app._build_styles()
            self.assertEqual(app.ui_colors["bg_primary"], dark["00"])
            self.assertEqual(app.ui_colors["text_normal"], dark["100"])
            # 窗口本体也得是暗的：winfo 拿到的是 Tk 记的底色，最能说明实际建成了哪套
            self.assertEqual(root.cget("background"), dark["00"])
            # 页面底色不能比卡片浅（暗色下浅一档就是「上面压着一条灰栏」）
            self.assertEqual(ttk.Style(root).lookup("Page.TFrame", "background"),
                             dark["00"])
        finally:
            root.destroy()

    def test_web_source_url_normalization(self):
        # 对话框的即时校验和写入走同一个判据，这里把边界钉住
        for raw in ("https://example.com/a?b=1", "  http://example.com  ", "https://例子.中国/文"):
            with self.subTest(raw=raw):
                self.assertEqual(
                    word_picker.normalize_web_source_url(raw), raw.strip()
                )
        for raw in ("", "   ", "ftp://example.com", "example.com", "https://",
                    "javascript:alert(1)", "file:///C:/x.md", "www.example.com"):
            with self.subTest(raw=raw):
                self.assertEqual(word_picker.normalize_web_source_url(raw), "")
        # 非字符串也不能炸：来源文本可能从配置或剪贴板来
        self.assertEqual(word_picker.normalize_web_source_url(None), "")

    def test_web_source_dialog_is_the_dark_ui(self):
        # 这里原本是 simpledialog.askstring —— 那是 Tk 经典对话框，跟着系统走、
        # 不读调色板，暗色界面里会弹一个浅色小窗。有人改回去或漏了底色/标题栏，
        # 这条会红。
        if word_picker.ui_theme is None or word_picker.ui_icons is None:
            self.skipTest("front 主题层没加载")
        try:
            root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(str(exc))
        root.withdraw()
        app = word_picker.WordPickerApp.__new__(word_picker.WordPickerApp)
        app.root = root
        app.ui_scale = 1.0
        app.lookup_popup = tk.Toplevel(root)          # _mark_web_source 要求它活着
        app.web_source_window = None
        dark = word_picker.ui_theme.BASE_DARK
        try:
            app._build_styles()
            app._mark_web_source()
            window = app.web_source_window
            root.update_idletasks()
            self.assertIsNotNone(window)
            self.assertTrue(window.winfo_exists())
            self.assertEqual(window.title(), "标注来源")
            self.assertEqual(window.cget("background"), dark["00"])
            self.assertEqual(str(window.attributes("-topmost")), "1")   # 父窗是 topmost

            found = {}

            def walk(widget):
                for child in widget.winfo_children():
                    found.setdefault(child.winfo_class(), []).append(child)
                    walk(child)

            walk(window)
            self.assertIn("TEntry", found)
            # 报错行要有自己的样式：用 Muted 就和上面那句说明同色，看不出是错误
            self.assertTrue(
                any(label.cget("style") == "Danger.TLabel"
                    for label in found.get("TLabel", [])),
                "校验提示应使用 Danger.TLabel",
            )
            buttons = found.get("TButton", [])
            self.assertTrue(
                any(button.cget("text") == "确定" for button in buttons),
                "对话框应有「确定」按钮",
            )
            self.assertTrue(
                any(button.cget("style") == "Accent.TButton" for button in buttons),
                "确定按钮应走强调样式",
            )
            # 重复触发只是把已有的窗提到前面，不该开出第二个
            app._mark_web_source()
            self.assertIs(app.web_source_window, window)
        finally:
            if app.web_source_window is not None:
                try:
                    app.web_source_window.grab_release()
                    app.web_source_window.destroy()
                except tk.TclError:
                    pass
            root.destroy()

    # -------------------------------------------------------------- 划词汇浮入口
    def test_float_entry_icon_asset_is_ready(self):
        # 主图规范（asset 规范见 assets/ui/icons 的其余 46 个）：48×48、RGBA、
        # 纯黑 + 透明底。IconCache._master() 只取 alpha 通道，所以 RGB 必须存在
        # 但内容无所谓——这里只断言 alpha 有实墨，断言 RGB 是黑反而会锁死实现。
        if word_picker.ui_icons is None:
            self.skipTest("front 主题层没加载")
        for mode, name in word_picker.FLOAT_ENTRY_ICONS.items():
            self.assertIn(mode, (word_picker.MODE_QUESTION, word_picker.MODE_HIGHLIGHT,
                                 word_picker.MODE_TRANSLATE))
            self.assertTrue((word_picker.ASSETS_UI_DIR / "icons" / f"{name}.png").exists())
        path = word_picker.ASSETS_UI_DIR / "icons" / "palette.png"
        self.assertTrue(path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n"))
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("没有 Pillow")
        with Image.open(path) as image:
            self.assertEqual(image.format, "PNG")
            self.assertEqual(image.mode, "RGBA")
            self.assertEqual(image.size, (48, 48))
            bbox = image.getchannel("A").getbbox()
        self.assertIsNotNone(bbox, "图标 alpha 全透明，等于没画")
        # 素材不需要登记：IconCache.names() 是直接读目录的
        self.assertIn(
            word_picker.FLOAT_ENTRY_ICONS[word_picker.MODE_HIGHLIGHT],
            word_picker.ui_icons.IconCache(
                str(word_picker.ASSETS_UI_DIR / "icons"), 1.0
            ).names(),
        )

    def test_float_entry_window_is_a_borderless_overlay(self):
        if word_picker.FloatingEntry is None or word_picker.ui_icons is None:
            self.skipTest("front 入口层没加载")
        try:
            root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(str(exc))
        root.withdraw()
        # 不要写死 1.0：这台机器就是 2.0，写死会让全部尺寸断言变成巧合
        scale = word_picker.ui_theme.detect_scale(root)
        icons = word_picker.ui_icons.IconCache(
            str(word_picker.ASSETS_UI_DIR / "icons"), scale
        )
        # 走的就是 _build_styles 那条路径，拿到的 colors 才和真实入口一致
        colors = word_picker.ui_theme.apply(
            root, palette=word_picker.ui_theme.default_palette(),
            scale=scale, icons=icons,
        )
        entry = word_picker.FloatingEntry(
            root, icons, scale=scale, colors=colors,
            icon_name=word_picker.FLOAT_ENTRY_ICONS[word_picker.MODE_HIGHLIGHT],
        )
        point = (400, 300)
        try:
            entry.show(*point)
            root.update()
            self.assertTrue(entry.is_visible())
            # 无边框 + 置顶：入口不该有标题栏，也不该被别的窗口盖住
            self.assertTrue(entry.window.overrideredirect())
            self.assertEqual(str(entry.window.attributes("-topmost")), "1")
            # 入口的左上角放在指针的右下方，指针必须落在窗口**外**——落在里面
            # 的话窗口一映射 <Enter> 就触发，用户看到的是「划完词菜单自己弹开」
            self.assertFalse(
                entry.contains(*point),
                f"入口压住了鼠标抬起点，会自触发。box="
                f"{entry.window.winfo_rootx()},{entry.window.winfo_rooty()}",
            )
            # 贴近屏幕边缘时是**翻转**到指针另一侧，不是钳制——钳制会把窗口拉回
            # 指针底下，照样自触发
            screen_w = root.winfo_screenwidth()
            edge = (screen_w - 3, 300)
            entry.show(*edge)
            root.update()
            self.assertFalse(entry.contains(*edge), "贴右边缘时入口压住了指针")
            self.assertLess(
                entry.window.winfo_rootx(), edge[0],
                "贴右边缘时应翻到指针左侧",
            )

            # 悬停展开出恰好三个颜色块
            swatches = [
                {"key": key, "kind": "swatch", "color": color}
                for key, color in (("yellow", "#e0ac00"), ("green", "#08b94e"),
                                   ("blue", "#086ddd"))
            ]
            entry.expand_items(swatches)
            root.update()
            self.assertEqual(len(entry.strip.winfo_children()), 3)
            # 高光色点纵向显现，图标本体屏幕位置不动
            self.assertTrue(entry._vertical)
            self.assertTrue(all(cell.winfo_children()[0].find_all()
                                for cell in entry.strip.winfo_children()))
            self.assertGreater(entry.window.winfo_height(), entry.icon_span)
            # 重复悬停不重建
            anchor = (entry.window.winfo_rootx(), entry.window.winfo_rooty())
            entry.expand_items(swatches)
            root.update()
            self.assertEqual(
                (entry.window.winfo_rootx(), entry.window.winfo_rooty()), anchor,
                "重复展开不该把图标挪走，也不该开出第二个窗",
            )
            entry.collapse()
            root.update()
            self.assertFalse(entry.strip.winfo_manager())
            cached_cells=list(entry.strip.winfo_children())
            entry.expand_items(swatches)
            root.update()
            self.assertEqual(list(entry.strip.winfo_children()), cached_cells,
                             "二次悬停应复用现有色点，不重建窗口控件")
            entry.collapse()
            entry.hide()
            root.update()
            self.assertFalse(entry.is_visible())
        finally:
            entry.destroy()
            self.assertFalse(entry.is_visible())
            root.destroy()

    def test_float_entry_tk_mouse_buttons_are_isolated(self):
        if word_picker.FloatingEntry is None or word_picker.ui_icons is None:
            self.skipTest("front 入口层没加载")
        try:
            root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(str(exc))
        root.withdraw()
        scale = word_picker.ui_theme.detect_scale(root)
        icons = word_picker.ui_icons.IconCache(
            str(word_picker.ASSETS_UI_DIR / "icons"), scale)
        colors = word_picker.ui_theme.apply(
            root, palette=word_picker.ui_theme.default_palette(),
            scale=scale, icons=icons)
        left, right = mock.Mock(), mock.Mock()
        entry = word_picker.FloatingEntry(root, icons, scale=scale, colors=colors,
            on_click=left, on_context_menu=right)
        try:
            entry.show(400, 300)
            root.update()
            entry.icon_label.event_generate("<Button-3>", x=3, y=3)
            root.update()
            right.assert_called_once()
            left.assert_not_called()
            entry.icon_label.event_generate("<Button-1>", x=3, y=3)
            root.update()
            left.assert_called_once()
            right.assert_called_once()
        finally:
            entry.destroy()
            root.destroy()

    def test_finish_capture_offers_float_entry_instead_of_opening_popup(self):
        # 本轮的行为反转：划词后**不再**直接弹大窗，窗口推迟到悬停时建。
        app = word_picker.WordPickerApp.__new__(word_picker.WordPickerApp)
        app.capture_mode = word_picker.MODE_QUESTION
        app.capture_revision = 7
        app.ui_icons = mock.Mock()
        app.ui_colors = {"bg_primary": "#1e1e1e", "border": "#333333"}
        app.root = mock.Mock()
        app.root.winfo_screenwidth.return_value = 1920
        app.root.winfo_screenheight.return_value = 1080
        app.float_entry = mock.Mock()
        app.float_entry.window = None
        app._begin_question_record = mock.Mock()
        app._begin_lookup = mock.Mock()
        app._prepared_words_for_sentence = mock.Mock(return_value=[{"word": "select"}])
        app.pending_capture = None
        app.capture_paused = False
        app.capture = mock.Mock()
        app.capture.clipboard_sequence.return_value = 99
        app.capture.read_clipboard_text.return_value = "选中的问题文本"
        app.config = mock.Mock(values={"minimum_text_length": 2})

        app._finish_capture(7, (320, 240), 7)

        app._begin_question_record.assert_not_called()
        app._begin_lookup.assert_not_called()
        self.assertEqual(app.pending_capture["text"], "选中的问题文本")
        self.assertEqual(app.pending_capture["point"], (320, 240))
        self.assertEqual(app.pending_capture["revision"], 7)
        # 入口显示期间 capture_paused 必须保持 False，否则新划词替换不掉旧入口
        self.assertFalse(getattr(app, "capture_paused", False))

        # 悬停只展开选项，**不执行任何动作**——这是本轮修的 bug：以前指针只是
        # 路过图标就把记录页面弹出来了
        app._on_float_entry_hover()
        app._begin_question_record.assert_not_called()
        app._begin_lookup.assert_not_called()
        self.assertIsNotNone(app.pending_capture)
        app.float_entry.expand_items.assert_not_called()

        # 右键只显示模式卡，不能同时打开记录页面或消费待处理选区。
        menu = mock.Mock()
        app._build_float_entry_menu = mock.Mock(return_value=menu)
        app._on_float_entry_menu(320, 240)
        menu.show.assert_called_once_with(320, 240)
        app._begin_question_record.assert_not_called()
        self.assertEqual(app.pending_capture["text"], "选中的问题文本")
        # 左键图标才打开本模式的记录页面，且只消费一次。
        app._on_float_entry_click(320, 240)
        app._begin_question_record.assert_called_once_with("选中的问题文本", (320, 240))
        self.assertIsNone(app.pending_capture)
        app.float_entry.hide.assert_called()

    def test_finish_capture_falls_back_to_popup_without_front_layer(self):
        # 入口层不可用（缺 Pillow / front 不完整）时必须退回「划词直接弹窗」，
        # 不能变成划完词什么也不发生——否则那些环境彻底用不了。
        app = word_picker.WordPickerApp.__new__(word_picker.WordPickerApp)
        app.capture_mode = word_picker.MODE_QUESTION
        app.capture_revision = 3
        app.ui_icons = None                      # 入口层不可用
        app.ui_colors = {"bg_primary": "#1e1e1e", "border": "#333333"}
        app.root = mock.Mock()
        app.float_entry = None
        app._begin_question_record = mock.Mock()
        app.pending_capture = None
        app.capture_paused = False
        app.capture = mock.Mock()
        app.capture.clipboard_sequence.return_value = 99
        app.capture.read_clipboard_text.return_value = "选中的问题文本"
        app.config = mock.Mock(values={"minimum_text_length": 2})

        app._finish_capture(3, (10, 20), 3)

        app._begin_question_record.assert_called_once_with("选中的问题文本", (10, 20))
        self.assertIsNone(app.pending_capture)

    def test_highlight_mode_has_swatches_and_a_page(self):
        # 菜单的 master 得是真窗口（Mock 没有 _last_child_ids），所以这条用真 Tk。
        try:
            root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(str(exc))
        root.withdraw()
        app = word_picker.WordPickerApp.__new__(word_picker.WordPickerApp)
        app.root = root
        app.ui_colors = {"yellow": "#e0ac00", "green": "#08b94e", "blue": "#086ddd"}
        app.capture_mode = word_picker.MODE_TRANSLATE
        app.mode_var = mock.Mock()
        app.status_label = mock.Mock()
        app.toggle_button = mock.Mock()
        app.monitoring = True
        app.float_entry = mock.Mock()
        app.float_entry.window = root
        app.highlight_active_colors = list(word_picker.DEFAULT_HIGHLIGHT_ACTIVE_COLORS)
        try:
            # 高光现在有主窗口页面了（本轮从「只有捕获语义」改成正常页面）
            self.assertIn(
                word_picker.MODE_HIGHLIGHT, word_picker.PAGE_LABELS.values()
            )
            # 悬浮窗只摆配置里选中的那 3 种（2.5.0 起可配），不是 8 种全上。
            self.assertEqual(
                [key for key, _ in app._highlight_swatches()],
                list(word_picker.DEFAULT_HIGHLIGHT_ACTIVE_COLORS),
            )
            # 色值与插件那张表一致；app.ui_colors 里那三个旧色值不再参与色板推导。
            self.assertEqual(
                [color for _, color in app._highlight_swatches()],
                ["#e0ac00", "#08b94e", "#086ddd"],
            )

            # 这个字面量会写进配置的 mode 字段（__init__ 从 config["mode"] 读回
            # capture_mode），改它等于让已经存过的配置读不回来
            self.assertEqual(word_picker.MODE_HIGHLIGHT, "highlight")

            # 三个模式现在都走 mode_var → _on_mode_changed 推导 capture_mode，
            # 上一轮那个「高光只改 capture_mode」的特例已经删掉
            app._switch_capture_mode(word_picker.MODE_HIGHLIGHT)
            app.mode_var.set.assert_called_once()
            self.assertEqual(
                word_picker.PAGE_LABELS[app.mode_var.set.call_args[0][0]],
                word_picker.MODE_HIGHLIGHT,
            )
            app.mode_var.set.reset_mock()

            app._switch_capture_mode(word_picker.MODE_QUESTION)
            app.mode_var.set.assert_called_once()
            self.assertEqual(
                word_picker.PAGE_LABELS[app.mode_var.set.call_args[0][0]],
                word_picker.MODE_QUESTION,
            )
            # 收起入口推到 after(0)：命令是在菜单派发过程中跑的，入口窗口是菜单的
            # owner，命令中途把它 withdraw 掉会让派发当场断掉（「点菜单没反应」）。
            app.float_entry.hide.assert_not_called()
            root.update()
            app.float_entry.hide.assert_called()

            # 右键菜单三项，值就是三个模式常量
            menu = app._build_float_entry_menu()
            try:
                labels = [menu.entrycget(i, "label")
                          for i in range(menu.index("end") + 1)]
                values = [menu.entrycget(i, "value")
                          for i in range(menu.index("end") + 1)]
            finally:
                menu.destroy()
            self.assertEqual(labels, ["记录问题", "记录单词", "高光模式"])
            self.assertEqual(
                values,
                [word_picker.MODE_QUESTION, word_picker.MODE_TRANSLATE,
                 word_picker.MODE_HIGHLIGHT],
            )
            self.assertEqual(word_picker.FLOAT_ENTRY_MODES,
                             tuple(zip(values, labels)))
            # 原生 tk.Menu 已被无边框主题卡片替换，三项仍保持原语义。
            menu = app._build_float_entry_menu()
            try:
                self.assertTrue(menu.window.wm_overrideredirect())
                menu.show(30, 30)
                self.assertTrue(menu.winfo_exists())
                menu._choose(word_picker.MODE_HIGHLIGHT)
                root.update()  # _switch_capture_mode 排了 after(0) 收起入口，销毁 root 前先跑完。
                self.assertFalse(menu.winfo_exists())
                self.assertEqual(app.mode_var.set.call_args.args[0],
                                 next(label for label, value in word_picker.PAGE_LABELS.items()
                                      if value == word_picker.MODE_HIGHLIGHT))
            finally:
                menu.close()
        finally:
            root.destroy()

    def test_theme_mode_popup_keeps_click_inside_and_closes_on_outside(self):
        try:
            root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(str(exc))
        root.withdraw()
        app = word_picker.WordPickerApp.__new__(word_picker.WordPickerApp)
        app.root = root
        app.float_entry = mock.Mock()
        app.float_entry.window = root
        app.float_entry.is_visible.return_value = True
        app.float_entry.contains.return_value = False
        app.capture_mode = word_picker.MODE_HIGHLIGHT
        app.ui_colors = word_picker.FALLBACK_UI_COLORS
        menu = app._build_float_entry_menu()
        try:
            app.float_entry_menu = menu
            menu.show(30, 30)
            root.update()
            inside = (menu.window.winfo_rootx() + 5, menu.window.winfo_rooty() + 5)
            app._on_pointer_down(inside)
            self.assertTrue(menu.winfo_exists())
            app.float_entry.hide.assert_not_called()
            app._on_pointer_down((root.winfo_screenwidth() - 2,
                                  root.winfo_screenheight() - 2))
            self.assertFalse(menu.winfo_exists())
            app.float_entry.hide.assert_called_once()
        finally:
            menu.close()
            root.destroy()

    def test_clicking_a_posted_menu_does_not_dismiss_the_entry(self):
        # 菜单是入口窗口的子窗口，点菜单项的这一次左键落在入口矩形之外。以前这会
        # 把入口 withdraw 掉，而它正是菜单的 owner——命令于是根本不派发，用户看到
        # 的就是「右键切换模式点了没反应，还是原来的功能」。
        try:
            root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(str(exc))
        root.withdraw()
        app = word_picker.WordPickerApp.__new__(word_picker.WordPickerApp)
        app.root = root
        app.float_entry = mock.Mock()
        app.float_entry.is_visible.return_value = True
        app.float_entry.contains.return_value = False
        menu = tk.Menu(root, tearoff=False)
        app.float_entry_menu = menu
        try:
            app._on_pointer_down((900, 900))
            app.float_entry.hide.assert_not_called()

            # 菜单收掉之后必须恢复：留着标记就等于把「点别处消失」永久关掉
            app.float_entry_menu = None
            app._on_pointer_down((900, 900))
            app.float_entry.hide.assert_called_once_with()
        finally:
            menu.destroy()
            root.destroy()

    def test_float_entry_hides_when_clicking_outside(self):
        app = word_picker.WordPickerApp.__new__(word_picker.WordPickerApp)
        app.float_entry = mock.Mock()
        app.float_entry.is_visible.return_value = True
        app.float_entry.contains.return_value = False

        app._on_pointer_down((500, 500))
        app.float_entry.hide.assert_called_once_with()

        # 低层钩子看得见落在我们自己窗口上的点击，所以点在入口身上的要放行，
        # 否则点颜色块时入口会先被收掉
        app.float_entry.hide.reset_mock()
        app.float_entry.contains.return_value = True
        app._on_pointer_down((500, 500))
        app.float_entry.hide.assert_not_called()

        # 入口本来就不可见时不必打扰它
        app.float_entry.is_visible.return_value = False
        app._on_pointer_down((500, 500))
        app.float_entry.hide.assert_not_called()

    def test_pending_obsidian_source_waits_for_the_popup_to_map(self):
        # 那次 Obsidian 桥接往返带 1.5 秒超时。窗口推迟到悬停才建之后，来源识别
        # 早就完成了，于是它一度被塞进建窗路径——实测点击回调同步阻塞 181.9 ms，
        # 其中 85 ms 就是这次往返（对照组 98.1 ms）。这条钉住「窗口先出来」。
        try:
            root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(str(exc))
        app = word_picker.WordPickerApp.__new__(word_picker.WordPickerApp)
        app.root = root
        app._apply_pending_obsidian_source = mock.Mock()
        popup = tk.Toplevel(root)
        try:
            app._defer_pending_obsidian_source(popup)
            app._apply_pending_obsidian_source.assert_not_called()

            root.update()
            self.assertEqual(
                app._apply_pending_obsidian_source.call_count, 1,
                "窗口映射之后应当补上这笔欠账",
            )

            # <Map> 和 after 兜底两道都会想跑一次，去重必须生效
            time.sleep(0.2)
            root.update()
            self.assertEqual(
                app._apply_pending_obsidian_source.call_count, 1,
                "映射与兜底各跑了一遍，欠账被补了两次",
            )
        finally:
            popup.destroy()
            root.destroy()

    def test_pending_obsidian_source_is_dropped_if_popup_is_gone(self):
        try:
            root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(str(exc))
        app = word_picker.WordPickerApp.__new__(word_picker.WordPickerApp)
        app.root = root
        app._apply_pending_obsidian_source = mock.Mock()
        popup = tk.Toplevel(root)
        try:
            app._defer_pending_obsidian_source(popup)
            popup.destroy()             # 用户在建窗前就把窗口关了
            root.update()
            time.sleep(0.2)
            root.update()
            app._apply_pending_obsidian_source.assert_not_called()
        finally:
            root.destroy()

    # ------------------------------------------------------------ 悬停选项
    def test_float_entry_hover_only_expands_highlight_colors(self):
        app = word_picker.WordPickerApp.__new__(word_picker.WordPickerApp)
        app.ui_colors = {"yellow": "#e0ac00", "green": "#08b94e", "blue": "#086ddd"}
        app.float_entry = mock.Mock()
        for mode in (word_picker.MODE_QUESTION, word_picker.MODE_TRANSLATE):
            app.capture_mode = mode
            app._on_float_entry_hover()
            app.float_entry.expand_items.assert_not_called()
        self.assertEqual(word_picker.FLOAT_ENTRY_ICONS[word_picker.MODE_QUESTION], "pencil")
        self.assertEqual(word_picker.FLOAT_ENTRY_ICONS[word_picker.MODE_TRANSLATE], "feather")

        app.capture_mode = word_picker.MODE_HIGHLIGHT
        app.highlight_active_colors = list(word_picker.DEFAULT_HIGHLIGHT_ACTIVE_COLORS)
        app._on_float_entry_hover()
        items = app.float_entry.expand_items.call_args.args[0]
        # 只摆配置的那 3 种（8 选 3），顺序就是配置顺序。
        self.assertEqual([item["key"] for item in items],
                         list(word_picker.DEFAULT_HIGHLIGHT_ACTIVE_COLORS))
        self.assertEqual([item["kind"] for item in items], ["swatch"] * 3)
        self.assertEqual([item["color"] for item in items],
                         [word_picker.DEFAULT_HIGHLIGHT_PALETTE[key]
                          for key in word_picker.DEFAULT_HIGHLIGHT_ACTIVE_COLORS])
        self.assertEqual(word_picker.FLOAT_ENTRY_ICONS[word_picker.MODE_HIGHLIGHT], "palette")

    def test_float_entry_right_click_only_switches_mode(self):
        app = word_picker.WordPickerApp.__new__(word_picker.WordPickerApp)
        app.pending_capture = {"text": "选区", "point": (10, 10)}
        app.float_entry = mock.Mock()
        app._begin_question_record = mock.Mock()
        app._begin_lookup = mock.Mock()
        first, second, third = mock.Mock(), mock.Mock(), mock.Mock()
        app._build_float_entry_menu = mock.Mock(side_effect=[first, second, third])
        for mode, popup in ((word_picker.MODE_QUESTION, first),
                            (word_picker.MODE_TRANSLATE, second),
                            (word_picker.MODE_HIGHLIGHT, third)):
            app.capture_mode = mode
            app._on_float_entry_menu(11, 12)
            popup.show.assert_called_once_with(11, 12)
            app._begin_question_record.assert_not_called()
            app._begin_lookup.assert_not_called()
            self.assertEqual(app.pending_capture["text"], "选区")
        first.close.assert_called_once()
        second.close.assert_called_once()

    def test_float_entry_left_click_records_only_in_record_modes(self):
        app = word_picker.WordPickerApp.__new__(word_picker.WordPickerApp)
        app._on_float_entry_menu = mock.Mock()
        app._open_pending_capture = mock.Mock()
        for mode in (word_picker.MODE_QUESTION, word_picker.MODE_TRANSLATE):
            app.capture_mode = mode
            app._on_float_entry_click(11, 12)
        self.assertEqual(app._open_pending_capture.call_count, 2)
        app._on_float_entry_menu.assert_not_called()
        app.capture_mode = word_picker.MODE_HIGHLIGHT
        app._on_float_entry_click(11, 12)
        self.assertEqual(app._open_pending_capture.call_count, 2,
                         "高光模式图标左键不应意外打开记录页面")

    def test_float_entry_pick_dispatches_by_key(self):
        app = word_picker.WordPickerApp.__new__(word_picker.WordPickerApp)
        app.float_entry = mock.Mock()
        app.pending_capture = {"text": "x", "point": (1, 2), "revision": 1}
        app.capture_mode = word_picker.MODE_TRANSLATE
        app._begin_lookup = mock.Mock()
        app._begin_question_record = mock.Mock()
        app._apply_highlight = mock.Mock()

        # 记录动作不再是展开项；旧 key 不能意外打开页面。
        app._on_float_entry_pick(word_picker.MODE_TRANSLATE)
        app._begin_lookup.assert_not_called()
        app._apply_highlight.assert_not_called()

        # 颜色键 → 就地上色，不开页面
        app.pending_capture = {"text": "x", "point": (1, 2), "revision": 1}
        app._on_float_entry_pick("green")
        app._apply_highlight.assert_called_once_with("green")
        app._begin_question_record.assert_not_called()

    def test_float_entry_rebuilds_items_only_when_content_changes(self):
        # 「内容没变就不重建」是上一轮把一次进出从 58 ms 压到 4.71 ms 的那条优化，
        # 加了 label 之后指纹必须跟着变，否则换了文案界面还留着旧字。
        if word_picker.FloatingEntry is None or word_picker.ui_icons is None:
            self.skipTest("front 入口层没加载")
        try:
            root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(str(exc))
        root.withdraw()
        scale = word_picker.ui_theme.detect_scale(root)
        icons = word_picker.ui_icons.IconCache(
            str(word_picker.ASSETS_UI_DIR / "icons"), scale
        )
        colors = word_picker.ui_theme.apply(
            root, palette=word_picker.ui_theme.default_palette(),
            scale=scale, icons=icons,
        )
        entry = word_picker.FloatingEntry(root, icons, scale=scale, colors=colors)
        try:
            entry.show(400, 300)
            root.update()
            first = [{"key": "question", "kind": "action", "label": "记录问题",
                      "icon": "message-square-plus"}]
            entry.expand_items(first)
            root.update()
            cells = list(entry.strip.winfo_children())
            self.assertEqual(len(cells), 1)

            entry.expand_items(first)
            root.update()
            self.assertEqual(
                list(entry.strip.winfo_children()), cells,
                "内容没变却又重建了一遍——指针每次扫过入口都要付这个成本",
            )

            entry.expand_items([{**first[0], "label": "记录知识点"}])
            root.update()
            self.assertNotEqual(
                list(entry.strip.winfo_children()), cells,
                "文案变了却没重建，界面上会留着旧文字",
            )
        finally:
            entry.destroy()
            root.destroy()

    # ------------------------------------------------------------ 高光
    def test_obsidian_bridge_client_applies_highlight(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary) / "vault"
            plugin_dir = (
                vault / ".obsidian" / "plugins" / word_picker.OBSIDIAN_PLUGIN_ID
            )
            plugin_dir.mkdir(parents=True)
            (plugin_dir / "data.json").write_text(
                json.dumps({"token": "secret-token", "port": 27124}),
                encoding="utf-8",
            )
            response = mock.MagicMock()
            response.__enter__.return_value.read.return_value = json.dumps(
                {
                    "kind": "obsidian",
                    "file_path": "课程/第一章.md",
                    "file_name": "第一章.md",
                    # 改成浮层之后不再返回块 ID，返回的是插件里那条锚点的标识
                    "highlight_id": "hl-abc123",
                    "color": "#e0ac00",
                }
            ).encode("utf-8")

            with mock.patch.object(
                word_picker.urllib.request, "urlopen", return_value=response
            ) as urlopen:
                result = word_picker.ObsidianBridgeClient(vault).apply_highlight(
                    "capture-1", "#e0ac00"
                )

            request = urlopen.call_args.args[0]
            self.assertEqual(request.full_url, "http://127.0.0.1:27124/highlight")
            self.assertEqual(
                json.loads(request.data.decode("utf-8")),
                {"capture_id": "capture-1", "color": "#e0ac00"},
            )
            self.assertEqual(result["highlight_id"], "hl-abc123")

    def test_highlight_pipe_success_skips_http(self):
        client = word_picker.ObsidianBridgeClient.__new__(word_picker.ObsidianBridgeClient)
        client.port, client.token = 27124, "test-token"
        result = {"highlight_id": "pipe-1"}
        with mock.patch.object(client, "_pipe_request", return_value=result) as pipe, \
                mock.patch.object(word_picker.urllib.request, "urlopen") as http:
            self.assertIs(client._request("/highlight", {"capture_id": "c1"}), result)
        pipe.assert_called_once_with("/highlight", {"capture_id": "c1"}, timeout=1.5)
        http.assert_not_called()

    def test_highlight_pipe_unavailable_falls_back_before_send(self):
        client = word_picker.ObsidianBridgeClient.__new__(word_picker.ObsidianBridgeClient)
        client.port, client.token = 27124, "test-token"
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b'{"highlight_id":"http-1"}'
        with mock.patch.object(client, "_pipe_request", return_value=None), \
                mock.patch.object(word_picker.urllib.request, "urlopen", return_value=response) as http:
            self.assertEqual(client._request("/highlight", {"capture_id": "c2"}),
                             {"highlight_id": "http-1"})
        sent = http.call_args.args[0]
        self.assertEqual(sent.full_url, "http://127.0.0.1:27124/highlight")
        self.assertEqual(sent.get_header("Authorization"), "Bearer test-token")

    def test_highlight_trace_reports_http_fallback_without_source_data(self):
        client = word_picker.ObsidianBridgeClient.__new__(word_picker.ObsidianBridgeClient)
        client.port, client.token = 27124, "test-token"
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b'{"highlight_id":"http-1"}'
        payload = {"capture_id": "c2", "trace": True}
        def unavailable(*_args, **_kwargs):
            client._highlight_pipe_fallback_reason = "connect_winerror_231"
            return None
        with mock.patch.object(client, "_pipe_request", side_effect=unavailable), \
                mock.patch.object(word_picker.urllib.request, "urlopen", return_value=response):
            result = client._request("/highlight", payload)
        self.assertEqual(result["_bridge_channel"], "http")
        self.assertEqual(result["_bridge_transport"], "http_fallback")
        self.assertEqual(result["_bridge_fallback_reason"], "connect_winerror_231")
        self.assertIs(result["_bridge_connect_reused"], False)
        self.assertIsNone(result["_bridge_wait_response_ms"])
        self.assertEqual(result["_pipe_fallback_reason"], "connect_winerror_231")
        self.assertIsInstance(result["_http_started_wall_ms"], int)
        self.assertNotIn("_bridge_transport", payload)

    def test_highlight_pipe_after_send_disconnect_does_not_replay_http(self):
        client = word_picker.ObsidianBridgeClient.__new__(word_picker.ObsidianBridgeClient)
        client.port, client.token = 27124, "test-token"
        with mock.patch.object(client, "_pipe_request", side_effect=
                               word_picker.ObsidianBridgeTransportError("结果尚未确认")), \
                mock.patch.object(word_picker.urllib.request, "urlopen") as http:
            with self.assertRaisesRegex(word_picker.ObsidianBridgeTransportError,
                                        "结果尚未确认"):
                client._request("/highlight", {"capture_id": "c3"})
        http.assert_not_called()

    def test_native_highlight_entry_avoids_desktop_clipboard_capture(self):
        app = word_picker.WordPickerApp.__new__(word_picker.WordPickerApp)
        app.monitoring = True
        app.capture_mode = word_picker.MODE_HIGHLIGHT
        app._native_highlight_ready = True
        app.capture_paused = False
        app.capture = mock.Mock()
        app._on_drag((320, 240), types.SimpleNamespace(process_name="Obsidian.exe"))
        app.capture.clipboard_sequence.assert_not_called()
        app.capture.send_copy.assert_not_called()

    def test_native_entry_configuration_uses_control_request_not_highlight(self):
        client = word_picker.ObsidianBridgeClient.__new__(word_picker.ObsidianBridgeClient)
        client._request = mock.Mock(return_value={"enabled": True})
        self.assertEqual(client.configure_highlight_entry(True), {"enabled": True})
        client._request.assert_called_once_with("/highlight-entry", {"enabled": True}, timeout=5)

    def test_highlight_source_detection_rejects_non_obsidian_windows(self):
        app = word_picker.WordPickerApp.__new__(word_picker.WordPickerApp)
        app.events = mock.Mock()
        app.highlight_source_revision = -1
        app.highlight_source_context = None
        app.highlight_source_error = ""

        app._start_highlight_source_detection(
            5, mock.Mock(process_name="chrome.exe")
        )

        self.assertIsNone(app.highlight_source_context)
        self.assertIn("只支持 Obsidian", app.highlight_source_error)
        # 判不出来就当场给结论，不必为此起线程、也不必去读配置
        app.events.put.assert_not_called()

    def test_highlight_source_ready_ignores_stale_revisions(self):
        app = word_picker.WordPickerApp.__new__(word_picker.WordPickerApp)
        app.highlight_source_revision = 9
        app.highlight_source_context = None
        app.highlight_source_error = ""

        app._handle_highlight_source_ready(8, mock.Mock(capture_id="old"))
        self.assertIsNone(app.highlight_source_context, "旧一轮的结果不该覆盖当前状态")

        context = mock.Mock(capture_id="new")
        app._handle_highlight_source_ready(9, context)
        self.assertIs(app.highlight_source_context, context)

    def test_highlight_without_obsidian_source_reports_instead_of_writing(self):
        app = word_picker.WordPickerApp.__new__(word_picker.WordPickerApp)
        app.float_entry = mock.Mock()
        app.status_label = mock.Mock()
        app.ui_colors = {"yellow": "#e0ac00"}
        app.highlight_source_context = None
        app.highlight_source_error = "高光只支持 Obsidian 本地 Markdown 文档，当前来源不支持。"
        app.pending_capture = {"text": "x", "point": (1, 2), "revision": 1}

        with mock.patch.object(word_picker.threading, "Thread") as thread:
            app._apply_highlight("yellow")

        thread.assert_not_called()
        self.assertIn("只支持 Obsidian",
                      app.status_label.configure.call_args.kwargs["text"])
        # 来源不支持时把捕获留着：用户换回别的模式还能用它
        self.assertIsNotNone(app.pending_capture)

    def test_highlight_click_waits_for_bridge_instead_of_claiming_unsupported_source(self):
        app = word_picker.WordPickerApp.__new__(word_picker.WordPickerApp)
        app.float_entry = mock.Mock()
        app.status_label = mock.Mock()
        app.ui_colors = {"yellow": "#e0ac00"}
        app.highlight_source_revision = 7
        app.highlight_source_context = None
        app.highlight_source_error = ""
        app.highlight_source_pending = True
        app.highlight_requested_key = None
        app.pending_capture = {"text": "刚划选的文字", "point": (1, 2), "revision": 7}

        with mock.patch.object(word_picker.threading, "Thread") as thread:
            app._apply_highlight("yellow")
            thread.assert_not_called()
            self.assertEqual(app.highlight_requested_key, "yellow")
            app.float_entry.flash.assert_not_called()
            self.assertIn("正在读取", app.status_label.configure.call_args.kwargs["text"])

            context = mock.Mock(capture_id="captured")
            app._handle_highlight_source_ready(7, context)
            thread.assert_called_once()
            self.assertIsNone(app.pending_capture)
            self.assertIsNone(app.highlight_requested_key)
            self.assertFalse(app.highlight_source_pending)

    def test_highlight_waiting_failure_reports_bridge_reason_not_source_guess(self):
        app = word_picker.WordPickerApp.__new__(word_picker.WordPickerApp)
        app.highlight_source_revision = 7
        app.highlight_source_pending = True
        app.highlight_requested_key = "green"
        app.pending_capture = {"text": "文字", "point": (3, 4), "revision": 7}
        app._report_highlight_error = mock.Mock()
        app._apply_highlight = mock.Mock()

        app._handle_highlight_source_ready(7, None, "Obsidian 中没有有效选区。")
        app._apply_highlight.assert_not_called()
        app._report_highlight_error.assert_called_once_with(
            "高光失败：Obsidian 中没有有效选区。", (3, 4)
        )
        # 旧一轮异步结果不能再执行新划选的色块。
        app.highlight_source_revision = 8
        app._handle_highlight_source_ready(7, mock.Mock(capture_id="stale"))
        app._apply_highlight.assert_not_called()

    def test_highlight_dispatches_to_worker_with_capture_and_color(self):
        app = word_picker.WordPickerApp.__new__(word_picker.WordPickerApp)
        app.float_entry = mock.Mock()
        app.status_label = mock.Mock()
        app.ui_colors = {"yellow": "#e0ac00"}
        context = mock.Mock(capture_id="capture-1")
        app.highlight_source_context = context
        app.highlight_source_error = ""
        pending = {"text": "被划选的文本", "point": (1, 2), "revision": 1}
        app.pending_capture = pending

        with mock.patch.object(word_picker.threading, "Thread") as thread:
            app._apply_highlight("yellow")

        thread.assert_called_once()
        # 文本也要传下去：PDF 的那条路没有 capture_id，索引里的原文只能用划词
        # 那一轮读到的这段文字。
        # 第三个参数是稳定颜色键：具体色值由插件调色板决定。
        self.assertEqual(
            thread.call_args.kwargs["args"],
            (context, "yellow", "yellow", "被划选的文本", (1, 2)),
        )
        self.assertIsNone(app.pending_capture)

    def test_tag_arrow_release_does_not_reset_highlighted_suggestion(self):
        support = word_picker.ui_tags.TagTextSupport.__new__(word_picker.ui_tags.TagTextSupport)
        support.popup = mock.Mock()
        support.popup.winfo_exists.return_value = True
        support.listbox = mock.Mock()
        support.matches = ["甲", "乙", "丙"]
        support.active = 0
        support._update_popup = mock.Mock()
        self.assertEqual(support._move(1), "break")
        support._on_key_release(types.SimpleNamespace(keysym="Down"))
        self.assertEqual(support.active, 1)
        support._update_popup.assert_not_called()
        support._on_key_release(types.SimpleNamespace(keysym="a"))
        support._update_popup.assert_called_once()

    def test_keyboard_text_paste_does_not_report_missing_image(self):
        app = word_picker.WordPickerApp.__new__(word_picker.WordPickerApp)
        app._set_question_image_error = mock.Mock()
        app._insert_question_image = mock.Mock()
        editor = mock.Mock()
        event = types.SimpleNamespace(widget=editor)
        with mock.patch.object(word_picker, "ImageGrab") as grab:
            grab.grabclipboard.return_value = None
            self.assertIsNone(app._paste_question_image(event))
            app._set_question_image_error.assert_not_called()
            self.assertIsNone(app._paste_question_image())
            app._set_question_image_error.assert_called_once_with("剪贴板中没有图片。")
            picture = mock.Mock()
            picture.convert.return_value = "rgba"
            grab.grabclipboard.return_value = picture
            with mock.patch.object(word_picker, "Image", object()):
                self.assertEqual(app._paste_question_image(event), "break")
            self.assertIs(app.question_image_target, editor)
            app._insert_question_image.assert_called_once_with(
                {"path": None, "data": "rgba", "name": "剪贴板图片"}
            )

    # ------------------------------------------------------------ 高光配置页
    def test_highlight_page_switches_capture_mode(self):
        # 高光从「只有捕获语义、没有页面」变成了正常页面：点导航就等于切模式
        page = word_picker.PAGE_LABELS["高光"]
        self.assertEqual(page, word_picker.MODE_HIGHLIGHT)
        self.assertEqual(word_picker.PAGE_ICONS["高光"], "highlighter")
        self.assertIn(
            "highlighter",
            word_picker.ui_icons.IconCache(
                str(word_picker.ASSETS_UI_DIR / "icons"), 1.0
            ).names(),
        )

        app = word_picker.WordPickerApp.__new__(word_picker.WordPickerApp)
        app.capture_mode = word_picker.MODE_TRANSLATE
        app.mode_var = mock.Mock()
        app.mode_var.get.return_value = "高光"
        app.page_frames = {word_picker.MODE_HIGHLIGHT: mock.Mock()}
        app._refresh_nav = mock.Mock()
        app._sync_status = mock.Mock()

        app._on_mode_changed()

        self.assertEqual(app.capture_mode, word_picker.MODE_HIGHLIGHT)
        app.page_frames[word_picker.MODE_HIGHLIGHT].tkraise.assert_called_once_with()

    def test_latency_probe_correlates_hook_to_color_click(self):
        app = word_picker.WordPickerApp.__new__(word_picker.WordPickerApp)
        app.pending_capture = {"text": "重点", "point": (4, 5), "revision": 1}
        app.highlight_source_context = mock.Mock(capture_id="capture-probe", file_path="a.md")
        app.highlight_source_pending = False
        app.float_entry = mock.Mock()
        app.float_entry.contains.return_value = True
        app._hide_float_entry = mock.Mock()
        app.capture = mock.Mock()
        app.capture.last_pointer_down = ((4, 5), time.perf_counter_ns() - 10_000_000,
                                         time.time_ns() // 1_000_000 - 10, 2)
        with tempfile.TemporaryDirectory() as folder:
            flag = Path(folder) / "enabled"
            flag.touch()
            with mock.patch.object(word_picker, "HIGHLIGHT_PERF_FLAG", flag), \
                 mock.patch.object(word_picker, "record_highlight_ui_event"), \
                 mock.patch.object(word_picker.threading, "Thread"):
                app._apply_highlight("yellow")
        probe = app._highlight_perf_pending["capture-probe"]
        self.assertEqual(probe["os_to_hook_ms"], 2)
        self.assertGreaterEqual(probe["hook_to_tk_ms"], 10)
        self.assertIsNone(app.pending_capture)

    def test_latency_probe_separates_input_storage_and_frame(self):
        from tools.highlight_latency_probe import summarize
        report = summarize({"capture_id": "capture-1", "hook_wall_ms": 1000,
                            "os_to_hook_ms": 3, "hook_to_tk_ms": 65,
                            "tk_callback_wall_ms": 1065, "bridge_send_wall_ms": 1070,
                            "request_received_wall_ms": 1090,
                            "storage_commit_wall_ms": 1320,
                            "render_registered_wall_ms": 1400,
                            "visual_frame_wall_ms": 1416,
                            "visual_confirmed": True})
        self.assertEqual(report["hook_to_frame_ms"], 416)
        self.assertEqual(report["bridge_received_to_commit_ms"], 230)
        self.assertEqual(report["commit_to_render_registration_ms"], 80)
        self.assertIsNone(report["bridge_response_to_ui_status_ms"])
        self.assertEqual(report["bridge_transport"], "unknown")
        self.assertIsNone(report["pipe_connect_ms"])
        pipe = summarize({"bridge_send_wall_ms": 1000, "pipe_connected_wall_ms": 1008,
                          "pipe_written_wall_ms": 1011, "request_received_wall_ms": 1060,
                          "bridge_transport": "pipe"})
        self.assertEqual(pipe["pipe_connect_ms"], 8)
        self.assertEqual(pipe["pipe_write_ms"], 3)
        self.assertEqual(pipe["pipe_written_to_received_ms"], 49)
        self.assertEqual(pipe["bridge_transport"], "pipe")
        self.assertEqual(pipe["bridge_channel"], "unknown")  # legacy samples
        traced = summarize({"bridge_channel": "pipe", "bridge_connect_reused": True,
                            "bridge_pipe_connect_ms": 0.8, "bridge_send_ms": 1,
                            "bridge_wait_response_ms": 43})
        self.assertEqual(traced["bridge_channel"], "pipe")
        self.assertTrue(traced["bridge_connect_reused"])
        self.assertEqual(traced["bridge_wait_response_ms"], 43)
        http = summarize({"bridge_send_wall_ms": 1000, "http_started_wall_ms": 1015,
                          "request_received_wall_ms": 1090,
                          "pipe_fallback_reason": "connect_winerror_231",
                          "bridge_transport": "http_fallback"})
        self.assertEqual(http["pipe_attempt_before_http_ms"], 15)
        self.assertEqual(http["http_send_to_received_ms"], 75)
        self.assertEqual(http["pipe_fallback_reason"], "connect_winerror_231")
        retry = summarize({"tk_callback_wall_ms": 1000,
                           "bridge_first_send_wall_ms": 1080,
                           "bridge_first_error_wall_ms": 1082,
                           "bridge_retry_send_wall_ms": 4700,
                           "bridge_send_wall_ms": 4700,
                           "request_received_wall_ms": 4800,
                           "bridge_attempt_stage": "retry"})
        self.assertEqual(retry["tk_to_first_bridge_send_ms"], 80)
        self.assertEqual(retry["first_error_to_retry_send_ms"], 3618)
        self.assertEqual(retry["bridge_send_to_received_ms"], 100)
        self.assertEqual(retry["bridge_attempt_stage"], "retry")
        server = summarize({"pipe_written_wall_ms": 1000,
                            "pipe_first_data_callback_wall_ms": 1100,
                            "pipe_data_callback_wall_ms": 1100,
                            "pipe_frame_dequeued_wall_ms": 1120,
                            "request_received_wall_ms": 1124,
                            "storage_commit_wall_ms": 1130,
                            "memory_updated_wall_ms": 1131,
                            "annotation_cache_updated_wall_ms": 1132,
                            "palette_ready_wall_ms": 1133,
                            "editor_lookup_start_wall_ms": 1134,
                            "editor_dispatch_start_wall_ms": 1135,
                            "editor_dispatch_end_wall_ms": 1138,
                            "editor_lookup_end_wall_ms": 1139})
        self.assertEqual(server["pipe_written_to_data_callback_ms"], 100)
        self.assertEqual(server["pipe_callback_to_frame_dequeued_ms"], 20)
        self.assertEqual(server["pipe_frame_dequeued_to_handler_ms"], 4)
        self.assertEqual(server["commit_to_memory_updated_ms"], 1)
        self.assertEqual(server["editor_dispatch_ms"], 3)

    def test_ui_click_log_does_not_wait_for_slow_disk(self):
        entered, release = threading.Event(), threading.Event()
        def slow_log(*_args, **_kwargs):
            entered.set()
            release.wait(1)
        try:
            with mock.patch.object(word_picker, "record_highlight_event", side_effect=slow_log):
                start = time.perf_counter()
                word_picker.record_highlight_ui_event("color_click", color="yellow")
                self.assertLess(time.perf_counter() - start, 0.25)
                self.assertTrue(entered.wait(1))
        finally:
            release.set()
            word_picker._HIGHLIGHT_UI_LOG_QUEUE.join()

    def test_old_plugin_single_export_endpoint_has_actionable_message(self):
        with tempfile.TemporaryDirectory() as temp:
            vault = Path(temp)
            settings = vault / ".obsidian" / "plugins" / word_picker.OBSIDIAN_PLUGIN_ID
            settings.mkdir(parents=True)
            (settings / "data.json").write_text(json.dumps({"token": "x", "port": 27124}), encoding="utf-8")
            client = word_picker.ObsidianBridgeClient(vault)
            with mock.patch.object(client, "_request", side_effect=word_picker.ObsidianBridgeError("not found")):
                with self.assertRaisesRegex(word_picker.ObsidianBridgeError, "重载 2.4.0"):
                    client.configure_highlight_single_export("问题库/索引.md", "问题库/注释")

    def test_highlight_colors_are_eight_fixed_slots(self):
        # 桌面端不再校验、也不持有可编辑的调色板：色值由插件那张固定表说了算，
        # 这里只保留一份用于色板和菜单展示的副本，两边必须逐项对得上。
        self.assertEqual(list(word_picker.HIGHLIGHT_SWATCH_KEYS),
                         ["red", "orange", "yellow", "green",
                          "blue", "purple", "magenta", "gray"])
        self.assertFalse(hasattr(word_picker, "checked_highlight_palette"),
                         "旧的可编辑调色板校验已退役")
        plugin_source = (Path(__file__).parent / "obsidian-plugin" / "noting-helper-bridge"
                         / "source.js").read_text(encoding="utf-8")
        for key, (label, color, _dot) in word_picker.HIGHLIGHT_COLOR_DEFS.items():
            self.assertIn(f'{key}: {{ label: "{label}", hex: "{color}"', plugin_source,
                          f"{key} 的色名/色值和插件不一致")
        app = word_picker.WordPickerApp.__new__(word_picker.WordPickerApp)
        # 旧配置里的自定义色值（2.3.x 那套）不再影响色板。
        app.config = mock.Mock(values={"highlight_color": "yellow",
                                       "highlight_palette": {"yellow": "#AA5500"}})
        # 悬浮窗只摆配置里的 3 种；8 种仍是「可选项全集」，由插件那张表定义。
        app.highlight_active_colors = ["red", "orange", "magenta"]
        self.assertEqual(app._highlight_swatches()[0], ("red", "#e5484d"))
        self.assertEqual([key for key, _ in app._highlight_swatches()],
                         ["red", "orange", "magenta"])
        self.assertEqual(len(word_picker.HIGHLIGHT_SWATCH_KEYS), 8)

    def test_single_export_sync_failure_still_records_the_highlight(self):
        # 单条导出目标是侧栏导出图标的配置：推不上去不该挡着用户记录高光。
        with tempfile.TemporaryDirectory() as temp:
            vault = Path(temp)
            index = vault / "问题库" / "索引.md"
            note_dir = vault / "问题库" / "注释"
            app = word_picker.WordPickerApp.__new__(word_picker.WordPickerApp)
            app.events = mock.Mock()
            app.config = mock.Mock(values={
                "highlight_single_index_dir": str(index.parent),
                "highlight_single_index_filename": index.name,
                "highlight_single_note_dir": str(note_dir),
                "obsidian_vault_path": str(vault),
            })
            app._highlight_single_export_lock = threading.Lock()
            app._highlight_single_export_synced = None
            context = word_picker.ObsidianCaptureContext(
                vault_path=str(vault), capture_id="capture-single", file_path="a.md",
                file_name="a.md", selection="sample")
            with mock.patch.object(word_picker, "ObsidianBridgeClient") as client:
                client.return_value.configure_highlight_single_export.side_effect = (
                    word_picker.ObsidianBridgeError("端点不可用"))
                client.return_value.apply_highlight.return_value = {"highlight_id": "hl-1"}
                app._run_highlight(context, "yellow", "#e0ac00", "sample")
                client.return_value.apply_highlight.assert_called_once()
                self.assertEqual(
                    client.return_value.configure_highlight_single_export.call_args.args,
                    ("问题库/索引.md", "问题库/注释"),
                    "推给插件的是 Vault 相对路径，不是本机绝对路径")
            kinds = [item.args[0][0] for item in app.events.put.call_args_list]
            self.assertIn("highlight_single_export_sync", kinds, "同步失败要如实提示")
            self.assertIn("highlight_done", kinds, "高光本身仍然记下来了")

    def _single_export_target_app(self, vault):
        app = word_picker.WordPickerApp.__new__(word_picker.WordPickerApp)
        app.config = mock.Mock(values={
            "obsidian_vault_path": str(vault),
            "highlight_single_index_dir": str(vault / "索引"),
            "highlight_single_index_filename": "旧索引.md",
            "highlight_single_index_path": str(vault / "索引" / "旧索引.md"),
            "highlight_single_note_dir": str(vault / "注释"),
            "output_filename": "不要修改其它设置.md",
        })
        interpreter = tk.Tcl()
        app.highlight_single_index_dir_var = tk.StringVar(interpreter, str(vault / "索引"))
        app.highlight_single_index_filename_var = tk.StringVar(interpreter, "新索引.md")
        app.highlight_single_note_var = tk.StringVar(interpreter, str(vault / "注释"))
        app._set_transient_status = mock.Mock()
        app._start_highlight_single_export_sync = mock.Mock()
        app._highlight_single_export_lock = threading.Lock()
        app._highlight_single_export_synced = None
        return app

    def test_highlight_index_selection_saves_and_syncs_without_global_save(self):
        with tempfile.TemporaryDirectory() as temp:
            vault = Path(temp)
            app = self._single_export_target_app(vault)
            target = vault / "索引" / "新索引.md"
            app.highlight_single_index_history_targets = {target.name: target}
            app._on_highlight_single_index_history_selected()
            self.assertEqual(app._highlight_single_export_payload(), {
                "index_path": "索引/新索引.md", "note_dir": "注释"})
            self.assertEqual(app.config.values["highlight_single_index_path"], str(target))
            self.assertEqual(app.config.values["output_filename"], "不要修改其它设置.md")
            app.config.save.assert_called_once_with()
            app._start_highlight_single_export_sync.assert_called_once_with(notify=True)
            self.assertTrue(app._apply_highlight_single_export_target())
            self.assertEqual(app.config.save.call_count, 1)

    def test_highlight_typed_index_normalizes_suffix_and_persists_for_restart(self):
        with tempfile.TemporaryDirectory() as temp:
            vault = Path(temp)
            app = self._single_export_target_app(vault)
            values = app.config.values
            app.config = word_picker.AppConfig(vault / "config.json")
            app.config.values.update(values)
            app.config.values["highlight_vault_path"] = str(vault)
            app.highlight_single_index_filename_var.set("手动索引")
            self.assertTrue(app._apply_highlight_single_export_target())
            restored = word_picker.AppConfig(vault / "config.json")
            self.assertEqual(restored.values["highlight_single_index_filename"], "手动索引.md")
            self.assertEqual(app.highlight_single_index_filename_var.get(), "手动索引.md")

    def test_highlight_invalid_or_incomplete_target_does_not_replace_saved_target(self):
        with tempfile.TemporaryDirectory() as temp:
            vault = Path(temp)
            app = self._single_export_target_app(vault)
            before = dict(app.config.values)
            for filename, directory, notes in (
                ("../外面.md", str(vault / "索引"), str(vault / "注释")),
                ("新索引.md", str(vault.parent), str(vault / "注释")),
                ("新索引.md", str(vault / "索引"), ""),
                ("新索引.md", "", ""),
            ):
                app.highlight_single_index_filename_var.set(filename)
                app.highlight_single_index_dir_var.set(directory)
                app.highlight_single_note_var.set(notes)
                self.assertFalse(app._apply_highlight_single_export_target())
                self.assertEqual(app.config.values, before)
            app.config.save.assert_not_called()
            app._start_highlight_single_export_sync.assert_not_called()

    def test_highlight_target_save_failure_rolls_back_without_sync(self):
        with tempfile.TemporaryDirectory() as temp:
            app = self._single_export_target_app(Path(temp))
            before = dict(app.config.values)
            app.config.save.side_effect = OSError("磁盘写入失败")
            self.assertFalse(app._apply_highlight_single_export_target())
            self.assertEqual(app.config.values, before)
            app._start_highlight_single_export_sync.assert_not_called()

    def test_highlight_clear_target_clears_legacy_path_and_syncs(self):
        with tempfile.TemporaryDirectory() as temp:
            app = self._single_export_target_app(Path(temp))
            for variable in (app.highlight_single_index_dir_var,
                             app.highlight_single_index_filename_var,
                             app.highlight_single_note_var):
                variable.set("")
            self.assertTrue(app._apply_highlight_single_export_target())
            self.assertEqual(app._highlight_single_index_path(), "")
            self.assertIsNone(app._highlight_single_export_payload())
            app._start_highlight_single_export_sync.assert_called_once_with(notify=True)

    def test_queued_highlight_target_sync_does_not_restore_old_index(self):
        with tempfile.TemporaryDirectory() as temp:
            vault = Path(temp)
            app = self._single_export_target_app(vault)
            old_payload = app._highlight_single_export_payload()
            self.assertTrue(app._apply_highlight_single_export_target())
            client = mock.Mock()
            app._ensure_highlight_single_export(client, str(vault), old_payload)
            client.configure_highlight_single_export.assert_called_once_with(
                "索引/新索引.md", "注释")
            self.assertFalse(app._highlight_single_export_pending())
            # A worker created for the previous vault must not write there either.
            with tempfile.TemporaryDirectory() as other:
                app.config.values["obsidian_vault_path"] = other
                app._ensure_highlight_single_export(client, str(vault), old_payload)
            self.assertEqual(client.configure_highlight_single_export.call_count, 1)

    def test_highlight_single_export_retries_until_the_plugin_matches(self):
        # 推给插件的时机原先只有保存/检测连接/记录高光三处，任一次失败都会让插件长期
        # 停在旧目标上。这里盯住兜底重试：没对上就按节流重来，对上了就彻底安静。
        with tempfile.TemporaryDirectory() as temp:
            vault = Path(temp)
            index = vault / "问题库" / "索引.md"
            app = word_picker.WordPickerApp.__new__(word_picker.WordPickerApp)
            app.config = mock.Mock(values={
                "highlight_single_index_dir": str(index.parent),
                "highlight_single_index_filename": index.name,
                "highlight_single_note_dir": str(vault / "问题库" / "注释"),
                "obsidian_vault_path": str(vault),
            })
            app._highlight_single_export_lock = threading.Lock()
            app._highlight_single_export_synced = None
            app._highlight_single_export_check_at = 0.0
            app._highlight_single_export_retry_at = 0.0
            with mock.patch.object(app, "_start_highlight_single_export_sync") as sync:
                app._maybe_retry_highlight_single_export()
                self.assertEqual(sync.call_count, 1, "没对上就该重推")
                self.assertFalse(sync.call_args.kwargs["notify"], "兜底重试不该反复弹窗")
                app._maybe_retry_highlight_single_export()
                self.assertEqual(sync.call_count, 1, "5 秒内连看都不看")
                app._highlight_single_export_check_at = 0.0
                app._maybe_retry_highlight_single_export()
                self.assertEqual(sync.call_count, 1, "15 秒内不重复推")
                # 假定这次推成功：身份对上之后循环必须彻底安静。
                payload = app._highlight_single_export_payload()
                app._highlight_single_export_synced = app._highlight_single_export_identity(
                    str(vault), payload)
                app._highlight_single_export_check_at = 0.0
                app._highlight_single_export_retry_at = 0.0
                app._maybe_retry_highlight_single_export()
                self.assertEqual(sync.call_count, 1, "对上之后不该再推")

    def test_highlight_single_export_pending_tolerates_bad_config(self):
        # _maybe_retry… 挂在 60ms 的事件循环上，路径越界这类配置错误在保存时已经拦过，
        # 这里只能安静返回 False，绝不能让异常掀翻事件循环。
        app = word_picker.WordPickerApp.__new__(word_picker.WordPickerApp)
        app.config = mock.Mock(values={
            "highlight_single_index_dir": str(Path(tempfile.gettempdir()) / "外面"),
            "highlight_single_index_filename": "索引.md",
            "highlight_single_note_dir": str(Path(tempfile.gettempdir()) / "注释"),
            "obsidian_vault_path": str(Path(tempfile.gettempdir()) / "另一个仓库"),
        })
        app._highlight_single_export_lock = threading.Lock()
        app._highlight_single_export_synced = None
        self.assertFalse(app._highlight_single_export_pending())

    def test_palette_slots_stay_in_fixed_order(self):
        app = word_picker.WordPickerApp.__new__(word_picker.WordPickerApp)
        app.config = mock.Mock(values={"highlight_color": "blue",
                                       "highlight_palette": dict(word_picker.DEFAULT_HIGHLIGHT_PALETTE)})
        # 悬浮窗顺序 = 配置里的顺序，改配置就改顺序（用户自己排的，别擅自重排成 8 色表序）。
        app.highlight_active_colors = ["blue", "red", "gray"]
        self.assertEqual([key for key, _ in app._highlight_swatches()],
                         ["blue", "red", "gray"])

    def test_highlight_export_targets_survive_a_restart(self):
        # highlight_export_dir 原来不在 DEFAULTS 里，load() 的回填循环就跳过它：
        # 导出成功后虽然被 save() 写进文件，重启却读不回来。
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "config.json"
            first = word_picker.AppConfig(path)
            first.values["highlight_export_dir"] = str(Path(temp) / "高光库")
            first.values["highlight_single_index_dir"] = str(Path(temp) / "问题库")
            first.values["highlight_single_index_filename"] = "索引.md"
            first.values["highlight_single_index_path"] = str(Path(temp) / "问题库" / "索引.md")
            first.values["highlight_single_note_dir"] = str(Path(temp) / "问题库" / "注释")
            first.values["highlight_active_colors"] = ["red", "purple", "magenta"]
            first.save()
            second = word_picker.AppConfig(path)
            self.assertEqual(second.values["highlight_export_dir"], str(Path(temp) / "高光库"))
            self.assertEqual(second.values["highlight_single_index_dir"],
                             str(Path(temp) / "问题库"))
            self.assertEqual(second.values["highlight_single_index_filename"], "索引.md")
            self.assertEqual(second.values["highlight_single_index_path"],
                             str(Path(temp) / "问题库" / "索引.md"))
            self.assertEqual(second.values["highlight_single_note_dir"],
                             str(Path(temp) / "问题库" / "注释"))
            self.assertEqual(second.values["highlight_active_colors"],
                             ["red", "purple", "magenta"])
            # 2.4.0 那份配置只有一整条 index_path，没有 dir/filename：读回来要拆开回填，
            # 否则升级后单条导出看着就是「没配过」。
            path.write_text(json.dumps({
                "highlight_single_index_path": str(Path(temp) / "旧库" / "索引.md"),
            }), encoding="utf-8")
            third = word_picker.AppConfig(path)
            self.assertEqual(third.values["highlight_single_index_dir"], str(Path(temp) / "旧库"))
            self.assertEqual(third.values["highlight_single_index_filename"], "索引.md")
            # 非法/缺省的颜色配置一律回落到默认那 3 种，绝不放任 0 种或 4 种进悬浮窗。
            self.assertEqual(third.values["highlight_active_colors"],
                             list(word_picker.DEFAULT_HIGHLIGHT_ACTIVE_COLORS))
            # 退役的调色板键不再被读回，下一次保存也就从文件里消失。
            path.write_text(json.dumps({"highlight_palette": {"yellow": "#ff00ff"}}),
                            encoding="utf-8")
            fourth = word_picker.AppConfig(path)
            self.assertNotIn("highlight_palette", fourth.values)
            fourth.save()
            self.assertNotIn("highlight_palette",
                             json.loads(path.read_text(encoding="utf-8")))

    def test_export_requests_carry_the_color_filter(self):
        with tempfile.TemporaryDirectory() as temp:
            vault = Path(temp)
            settings = vault / ".obsidian" / "plugins" / word_picker.OBSIDIAN_PLUGIN_ID
            settings.mkdir(parents=True)
            (settings / "data.json").write_text(json.dumps({"token": "t", "port": 27124}),
                                                encoding="utf-8")
            client = word_picker.ObsidianBridgeClient(vault)
            with mock.patch.object(client, "_request", return_value={"count": 1}) as request:
                client.export_highlights("高光库")
                self.assertEqual(request.call_args.args[1], {"directory": "高光库"},
                                 "不选颜色就是导出全部，请求体里不带 color")
                client.export_highlights("高光库", "yellow")
                self.assertEqual(request.call_args.args[1],
                                 {"directory": "高光库", "color": "yellow"})
                # 颜色集走 GET（payload=None），每次点下拉现问插件。
                client.highlight_export_options()
                self.assertEqual(request.call_args.args, ("/highlight-export-options",))
                # 悬浮窗那 3 色走 POST /highlight-active-colors，请求体就是列表本身。
                client.configure_highlight_active_colors(["yellow", "green", "blue"])
                self.assertEqual(request.call_args.args,
                                 ("/highlight-active-colors",
                                  {"colors": ["yellow", "green", "blue"]}))
            # 旧插件的 404 要转成能看懂的话，和单条导出那条一个待遇。
            with mock.patch.object(client, "_request",
                                   side_effect=word_picker.ObsidianBridgeError("not found")):
                with self.assertRaisesRegex(word_picker.ObsidianBridgeError, "重载 2.5.0"):
                    client.configure_highlight_active_colors(["yellow", "green", "blue"])

    def test_highlight_active_colors_must_be_exactly_three_distinct_keys(self):
        # 悬浮窗摆几个圆点由这份配置说了算：0 个、4 个、重名、写错的颜色名
        # 都会让悬浮窗没法用，所以一律回落到默认那 3 种，而不是「尽力而为」。
        self.assertEqual(list(word_picker.HIGHLIGHT_COLOR_DEFS),
                         list(word_picker.HIGHLIGHT_SWATCH_KEYS))
        self.assertEqual(word_picker.HIGHLIGHT_ACTIVE_COLOR_COUNT, 3)
        for value in (None, "yellow", [], ["red", "purple"],
                      ["red", "purple", "gray", "blue"],
                      ["red", "red", "gray"], ["red", "purple", "chartreuse"],
                      ["red", "", "gray"], [None, "red", "gray"]):
            self.assertEqual(word_picker.normalize_highlight_active_colors(value),
                             list(word_picker.DEFAULT_HIGHLIGHT_ACTIVE_COLORS),
                             f"{value!r} 不是 3 个合法且互不相同的颜色名")
        # 合法就原样保留顺序——用户自己排的次序就是画出来的次序。
        self.assertEqual(word_picker.normalize_highlight_active_colors(
            ("gray", "red", "magenta")), ["gray", "red", "magenta"])
        self.assertEqual(word_picker.normalize_highlight_active_colors(
            ["  blue  ", "yellow", "green"]), ["blue", "yellow", "green"])

    def test_active_colors_only_push_when_the_vault_or_the_colors_change(self):
        # 每个保存动作都会走一遍这条链；颜色没变就别再打一次 HTTP。
        with tempfile.TemporaryDirectory() as temp:
            vault = Path(temp)
            app = word_picker.WordPickerApp.__new__(word_picker.WordPickerApp)
            app.config = mock.Mock(values={
                "highlight_active_colors": ["red", "purple", "magenta"],
                "highlight_vault_path": str(vault),
            })
            app._highlight_active_colors_lock = threading.Lock()
            app._highlight_active_colors_synced = None
            client = mock.Mock()
            app._ensure_highlight_active_colors(client, str(vault))
            client.configure_highlight_active_colors.assert_called_once_with(
                ["red", "purple", "magenta"])
            app._ensure_highlight_active_colors(client, str(vault))
            self.assertEqual(client.configure_highlight_active_colors.call_count, 1,
                             "同一次会话里颜色没变就不重推")
            # 配置改了：必须再推一次。
            app.config.values["highlight_active_colors"] = ["gray", "red", "blue"]
            app._ensure_highlight_active_colors(client, str(vault))
            self.assertEqual(client.configure_highlight_active_colors.call_count, 2)
            self.assertEqual(client.configure_highlight_active_colors.call_args.args,
                             (["gray", "red", "blue"],))
            # 换了仓库也要重推，`(仓库, 颜色)` 是一起的身份。
            with tempfile.TemporaryDirectory() as other:
                app.config.values["highlight_vault_path"] = other
                app._ensure_highlight_active_colors(client, other)
            self.assertEqual(client.configure_highlight_active_colors.call_count, 3)

    def test_active_colors_sync_failure_surfaces_in_the_status_line(self):
        app = word_picker.WordPickerApp.__new__(word_picker.WordPickerApp)
        app.events = queue.Queue()
        app.root = mock.Mock()
        app.root.winfo_exists.return_value = True
        app._set_transient_status = mock.Mock()
        # 成功时不抢状态栏（单条导出那条已经报过了），失败才说话。
        app.events.put(("highlight_active_colors_sync", True, ""))
        app._process_events()
        app._set_transient_status.assert_not_called()
        app.events.put(("highlight_active_colors_sync", False, "桥接没响应"))
        app._process_events()
        self.assertIn("桥接没响应", app._set_transient_status.call_args.args[0])

    def test_highlight_export_menu_lists_only_colors_in_use(self):
        try:
            root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(str(exc))
        root.withdraw()
        app = word_picker.WordPickerApp.__new__(word_picker.WordPickerApp)
        app.root = root
        button = ttk.Button(root, text="按颜色导出 ▾")
        button.pack()
        root.update()
        app.highlight_export_menu_button = button
        app._px = lambda value: max(1, int(value))
        app._highlight_swatch_images = {}
        app.highlight_export_button = ttk.Button(root, text="")
        scopes = []
        app._export_highlights = lambda scope="": scopes.append(scope)
        options = {"total": 12, "colors": [
            {"key": "red", "label": "红", "colorHex": "#e5484d", "dot": "🔴", "count": 0},
            {"key": "yellow", "label": "黄", "colorHex": "#e0ac00", "dot": "🟡", "count": 5},
            {"key": "blue", "label": "蓝", "colorHex": "#086ddd", "dot": "🔵", "count": 7},
        ]}
        try:
            with mock.patch.object(tk.Menu, "tk_popup") as popup, \
                 mock.patch.object(tk.Menu, "grab_release") as release:
                app._show_highlight_export_menu("vault", options)
            self.assertTrue(popup.called, "菜单要弹在按钮下面")
            self.assertTrue(release.called, "弹出后必须释放 grab，否则鼠标被菜单锁住")
            menu = app._highlight_export_menu
            self.assertIsInstance(menu, tk.Menu, "Menu 必须留引用，否则会被 GC 掉")
            # 菜单项下标要连分隔线一起数，别用「非分隔项列表」的下标去 invoke。
            labels = {}
            for index in range(menu.index("end") + 1):
                try:
                    labels[menu.entrycget(index, "label")] = index
                except tk.TclError:
                    continue  # 分隔线没有 label
            self.assertIn("所有 md (12)", labels)
            self.assertIn("md (5)", labels)
            self.assertIn("md (7)", labels)
            self.assertNotIn("md (0)", labels, "没有高光的颜色不进菜单")
            # 颜色不用文字表，改用纯色小方块贴图；「md (5)」这类文案区分不了黄蓝，
            # 认颜色全靠这张图，所以它必须真的挂上去。
            yellow_index = labels["md (5)"]
            self.assertEqual(menu.entrycget(yellow_index, "compound"), "left")
            self.assertTrue(menu.entrycget(yellow_index, "image"),
                            "颜色档必须带色块图（不能只有文字）")
            self.assertIsNotNone(app._highlight_swatch_images.get("yellow"),
                                 "PhotoImage 必须留引用，否则被 GC 后色块变空白")
            # 每项都要绑住自己的颜色键，不能全指向最后一项（闭包默认参数的坑）。
            menu.invoke(labels["md (5)"])
            self.assertEqual(scopes, ["yellow"])
            menu.invoke(labels["md (7)"])
            self.assertEqual(scopes, ["yellow", "blue"])
            menu.invoke(labels["所有 md (12)"])
            self.assertEqual(scopes, ["yellow", "blue", ""])
            # 换档之后按钮文案要跟着走：所有档写字，颜色档配同色小方块。
            self.assertEqual(app.highlight_export_button.cget("text"),
                             "导出索引与注释（所有 md）")
            app._select_highlight_export_scope("blue")
            self.assertEqual(scopes, ["yellow", "blue", "", "blue"])
            self.assertEqual(app.highlight_export_button.cget("text"), "导出索引与注释（md）")
            self.assertTrue(app.highlight_export_button.cget("image"))
            # 一条高光都没有时不报错，给一条禁用提示。
            with mock.patch.object(tk.Menu, "tk_popup"), mock.patch.object(tk.Menu, "grab_release"):
                app._show_highlight_export_menu("vault", {"total": 0, "colors": []})
            empty = app._highlight_export_menu
            self.assertEqual(empty.entrycget(0, "label"), "没有可导出的高光")
            self.assertEqual(empty.entrycget(0, "state"), "disabled")
        finally:
            root.destroy()

    def test_export_options_result_restores_the_menu_button(self):
        app = word_picker.WordPickerApp.__new__(word_picker.WordPickerApp)
        app.config = types.SimpleNamespace(values={"highlight_vault_path": "C:/vault"})
        app.events = queue.Queue()
        app.root = mock.Mock()
        app.root.winfo_exists.return_value = True
        app._set_transient_status = mock.Mock()
        app._show_highlight_export_menu = mock.Mock()
        button = mock.Mock()
        button.winfo_exists.return_value = True
        app.highlight_export_menu_button = button
        # 成功：恢复按钮并把结果交给菜单。
        app.events.put(("highlight_export_options", "C:/vault",
                        {"total": 3, "colors": []}, ""))
        app._process_events()
        self.assertEqual(button.state.call_args.args[0], ["!disabled"])
        app._show_highlight_export_menu.assert_called_once_with("C:/vault",
                                                               {"total": 3, "colors": []})
        # 插件不可达：按钮必须恢复可用，而且给出醒目提示，否则用户以为软件卡死了。
        app.events.put(("highlight_export_options", "C:/vault", {}, "桥接没响应"))
        with mock.patch.object(word_picker.messagebox, "showwarning") as warning:
            app._process_events()
        self.assertEqual(button.state.call_args.args[0], ["!disabled"])
        warning.assert_called_once()
        self.assertIn("桥接没响应", warning.call_args.args[1])

    def test_bridge_actions_live_at_sidebar_bottom_across_pages(self):
        try:
            root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(str(exc))
        root.withdraw()
        app = word_picker.WordPickerApp.__new__(word_picker.WordPickerApp)
        app._px = lambda value: value
        app.capture_mode = word_picker.MODE_QUESTION
        app.ui_icons = None
        app.nav_buttons = {}
        app.nav_icons = {}
        app.obsidian_status_labels = []
        app._on_mode_changed = mock.Mock()
        shell = ttk.Frame(root)
        try:
            app._build_sidebar(shell)
            bar = shell.winfo_children()[0]
            tools = [child for child in bar.winfo_children()
                     if isinstance(child, ttk.Frame) and child.pack_info().get("side") == "bottom"]
            self.assertEqual(len(tools), 1)
            buttons = [child for child in tools[0].winfo_children() if isinstance(child, ttk.Button)]
            self.assertEqual([button.cget("text") for button in buttons],
                             ["安装/更新插件", "检测连接"])
            app.mode_var.set("高光")
            self.assertTrue(all(button.winfo_exists() for button in buttons))
            app._set_obsidian_status("连接成功")
            self.assertEqual(app.obsidian_sidebar_status.cget("text"), "连接成功")
        finally:
            root.destroy()

    def test_settings_page_scrolls_under_pointer_without_stealing_text_wheel(self):
        try:
            root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(str(exc))
        root.geometry("400x220")
        app = word_picker.WordPickerApp.__new__(word_picker.WordPickerApp)
        app.root = root
        app.ui_colors = None
        app._px = lambda value: value
        holder = ttk.Frame(root)
        holder.pack(fill="both", expand=True)
        holder.rowconfigure(0, weight=1)
        holder.columnconfigure(0, weight=1)
        try:
            wrapper, page, canvas = app._scrollable_settings_page(holder)
            for index in range(40):
                ttk.Label(page, text=f"选项 {index}").pack(anchor="w", pady=5)
            app.mode_var = tk.StringVar(value="高光")
            app.page_frames = {word_picker.MODE_HIGHLIGHT: wrapper}
            app.settings_canvases = {word_picker.MODE_HIGHLIGHT: canvas}
            root.update()
            root.winfo_containing = lambda _x, _y: page
            event = types.SimpleNamespace(x_root=5, y_root=5, delta=-120)
            self.assertEqual(app._on_settings_wheel(event), "break")
            self.assertGreater(canvas.yview()[0], 0)
            root.winfo_containing = lambda _x, _y: tk.Text(page)
            self.assertIsNone(app._on_settings_wheel(event))
        finally:
            root.destroy()

    def test_highlight_page_only_configures_export_color_and_reset(self):
        if word_picker.FloatingEntry is None or word_picker.ui_icons is None:
            self.skipTest("front 层没加载")
        try:
            root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(str(exc))
        root.withdraw()
        scale = word_picker.ui_theme.detect_scale(root)
        # 刻意**不** apply 主题：theme.py 里的 element 图有存在性守卫，一旦在某个
        # 解释器上建过，后面换了 root 也不会重建，那些图会随本轮的 root 一起没了，
        # 后跑的用例再 apply 就会报 "image pyimageN doesn't exist"（踩过一次）。
        # 页面构建不依赖样式表，未定义的样式名 ttk 会用默认值兜住。
        app = word_picker.WordPickerApp.__new__(word_picker.WordPickerApp)
        app._px = lambda value: max(1, int(round(value * scale)))
        app.config = mock.Mock(values={
            "highlight_color": "green",
            "highlight_single_index_dir": "",
            "highlight_single_index_filename": "",
            "highlight_single_note_dir": "",
            "highlight_active_colors": list(word_picker.DEFAULT_HIGHLIGHT_ACTIVE_COLORS),
        })
        app.obsidian_vault_var = tk.StringVar()
        app.obsidian_status_labels = []
        for name in ("_register_obsidian_status_label",):
            setattr(app, name, types.MethodType(
                getattr(word_picker.WordPickerApp, name), app
            ))

        page = ttk.Frame(root)
        try:
            app._build_highlight_page(page)
            root.update()

            frames = [child for child in page.winfo_children()
                      if isinstance(child, ttk.LabelFrame)]
            self.assertEqual(
                [frame.cget("text") for frame in frames],
                ["Obsidian 仓库", "划选入口", "按需导出 Markdown",
                 "单条导出目标（侧栏每张卡片上的导出图标）", "高光颜色（8 选 3）", "清理高光"],
            )
            # Highlighter state must not reuse the question vault variable/status.
            self.assertIsNot(app.highlight_vault_var, app.obsidian_vault_var)
            self.assertEqual(len(app.obsidian_status_labels), 0)
            self.assertEqual(len(app._highlight_status_labels), 1)
            self.assertNotIn("检测连接", [child.cget("text") for child in frames[0].winfo_children()
                                      if isinstance(child, ttk.Button)])
            self.assertFalse(hasattr(app, "highlight_journal_var"))
            self.assertTrue(app.highlight_export_dir_var.get().endswith("导出"))
            export_buttons = [child for child in frames[2].winfo_children()
                              if isinstance(child, ttk.Button)]
            # 分体按钮：左键按当前档导出、▾ 换档。默认档是「所有 md」。
            self.assertEqual([button.cget("text") for button in export_buttons],
                             ["选择目录…", "导出索引与注释（所有 md）", "▾"])
            self.assertEqual(app._highlight_export_scope, "",
                             "当前档只在内存里，默认所有 md，不持久化")
            # 单条导出照搬「记录问题」：目录 + 文件名（可编辑下拉框），没有整条路径。
            self.assertIsNotNone(app.highlight_single_index_dir_var)
            self.assertIsNotNone(app.highlight_single_index_filename_var)
            self.assertIsNotNone(app.highlight_single_note_var)
            self.assertIsInstance(app.highlight_single_index_history_box, ttk.Combobox)
            self.assertEqual(str(app.highlight_single_index_history_box.cget("state")), "normal",
                             "要能直接手输新文件名")
            for event in ("<FocusOut>", "<Return>", "<<ComboboxSelected>>"):
                self.assertTrue(app.highlight_single_index_history_box.bind(event),
                                f"索引输入/切换必须绑定提交事件：{event}")
            for child in frames[3].winfo_children():
                if isinstance(child, ttk.Entry) and not isinstance(child, ttk.Combobox):
                    self.assertTrue(child.bind("<FocusOut>"))
                    self.assertTrue(child.bind("<Return>"))
            self.assertFalse(hasattr(app, "highlight_single_index_var"),
                             "整条路径那个入口已删（它的 asksaveasfilename 会弹覆盖确认）")
            self.assertFalse(hasattr(app, "highlight_single_title_var"))
            reset_buttons = [child for child in frames[5].winfo_children()
                             if isinstance(child, ttk.Button)]
            self.assertEqual([button.cget("text") for button in reset_buttons],
                             ["清空所有高光与注释…"])
            self.assertEqual(
                [key for key, _ in app._highlight_swatches()],
                list(word_picker.DEFAULT_HIGHLIGHT_ACTIVE_COLORS))
            # 颜色区：8 个色块 + 8 个色名，全部可点选（不是只读图例）。
            legend = frames[4].winfo_children()[0]
            self.assertEqual(len(legend.winfo_children()), 16)
            self.assertEqual(len(app.highlight_color_buttons), 8)
            for key, chip in app.highlight_color_buttons.items():
                self.assertTrue(chip.bind("<Button-1>"), f"{key} 色块必须可点选")
            # 默认选中的 3 个加边框打勾，其余是平面 + 中点占位。色名是经典 tk.Label
            # （选中态要改前景色，ttk 得配 style），所以按字典取而不是按类型筛子控件。
            self.assertEqual(
                [app.highlight_color_names[key].cget("text")
                 for key in word_picker.HIGHLIGHT_COLOR_DEFS],
                [f"{'✓' if key in word_picker.DEFAULT_HIGHLIGHT_ACTIVE_COLORS else '·'}"
                 f" {label} {dot}" for key, (label, _color, dot)
                 in word_picker.HIGHLIGHT_COLOR_DEFS.items()])
            # 色名和色块一样能点——只让 12px 的小方块可点，第一眼看不出来那是按钮。
            for key, name_label in app.highlight_color_names.items():
                self.assertTrue(name_label.bind("<Button-1>"), f"{key} 色名必须可点选")
                self.assertEqual(str(name_label.cget("cursor")), "hand2")
            # 勾的颜色必须跟着底色深浅走，否则黄块上打白勾等于没打。
            for key, chip in app.highlight_color_buttons.items():
                selected = key in word_picker.DEFAULT_HIGHLIGHT_ACTIVE_COLORS
                self.assertEqual(chip.cget("text"), "✓" if selected else "",
                                 f"{key} 只有选中的才打勾")
                if selected:
                    self.assertEqual(
                        str(chip.cget("foreground")),
                        word_picker.HIGHLIGHT_SWATCH_TEXT_COLORS[key],
                        f"{key} 的勾要用与底色对比的颜色")
            # 选满 3 个之后再点第 4 个只提示，不悄悄挤掉已选的。
            app._set_transient_status = mock.Mock()
            app._toggle_highlight_active_color("red")
            self.assertEqual(app.highlight_active_colors,
                             list(word_picker.DEFAULT_HIGHLIGHT_ACTIVE_COLORS))
            self.assertTrue(app._set_transient_status.called, "选满 3 个要提示")
            # 点掉一个再选就正常了。
            app._toggle_highlight_active_color("yellow")
            app._toggle_highlight_active_color("red")
            self.assertEqual(app.highlight_active_colors, ["green", "blue", "red"])
            self.assertFalse(hasattr(app, "highlight_palette_vars"))
        finally:
            root.destroy()

    def test_swatch_text_color_matches_the_plugin_stylesheet(self):
        """桌面端色块上的勾，和插件里色块的文字，必须是同一套对比色。

        两处各写一份、其中一处改了另一处没改，会出现「桌面端蓝底白勾、Obsidian 里
        蓝底白字」这种看不出毛病、但确实不一致的偏差。这里直接读插件的 styles.css 对账。
        """
        def _rgb(value):
            """`#fff` 和 `#ffffff` 是同一个颜色，比之前先摊成 6 位。"""
            value = value.strip().lstrip("#")
            if len(value) == 3:
                value = "".join(char * 2 for char in value)
            return tuple(int(value[index:index + 2], 16) for index in (0, 2, 4))

        css = (word_picker.APP_DIR / "obsidian-plugin" / "noting-helper-bridge"
               / "styles.css").read_text(encoding="utf-8")
        for key, hex_value in word_picker.DEFAULT_HIGHLIGHT_PALETTE.items():
            match = re.search(
                r"\.nh-highlight-swatches\s+\.nh-hl-" + key + r"\s*\{([^}]*)\}", css)
            self.assertIsNotNone(match, f"styles.css 里少了 {key} 的色块规则")
            body = match.group(1)
            self.assertIn(f"background: {hex_value}", body,
                          f"{key} 的底色两端必须一致")
            # 这里比的是 CSS 里那几个声明，`#fff` 和 `#ffffff` 都算过——所以别拿
            # 字符串直接比，读出来换成元组。
            wanted = word_picker.HIGHLIGHT_SWATCH_TEXT_COLORS[key]
            declared = re.findall(r"color:\s*(#[0-9a-fA-F]{3,6})", body)
            self.assertTrue(declared, f"{key} 的色块规则里没有 color 声明")
            self.assertIn(_rgb(wanted), [_rgb(value) for value in declared],
                          f"{key} 色块上的字/勾颜色两端必须一致")

    # ------------------------------------------------------------ 失败提示
    def test_highlight_failure_keeps_the_entry_and_flashes_the_reason(self):
        # 原来失败只写主窗口状态行：窗口最小化时用户什么都看不见，功能看着就像
        # 坏了（PDF 那次就是这么被误判的）。现在提示就落在刚点过的色块旁边。
        app = word_picker.WordPickerApp.__new__(word_picker.WordPickerApp)
        app.float_entry = mock.Mock()
        app.float_entry.is_visible.return_value = True
        app.status_label = mock.Mock()
        app.ui_colors = {"yellow": "#e0ac00"}
        app.highlight_source_context = None
        app.highlight_source_error = "高光暂不支持 Vault 里的 .docx 文件，只支持 Markdown 与 PDF。"
        app.pending_capture = {"text": "x", "point": (1, 2), "revision": 1}

        app._apply_highlight("yellow")

        app.float_entry.flash.assert_called_once()
        self.assertIn(".docx", app.float_entry.flash.call_args.args[0])
        # 入口不能收起：收了提示就没地方放了
        app.float_entry.hide.assert_not_called()
        self.assertIn(".docx", app.status_label.configure.call_args.kwargs["text"])

    def test_highlight_failure_does_not_blow_up_without_the_entry_layer(self):
        app = word_picker.WordPickerApp.__new__(word_picker.WordPickerApp)
        app.float_entry = None
        app.status_label = mock.Mock()
        app.ui_colors = {"yellow": "#e0ac00"}
        app.highlight_source_context = None
        app.highlight_source_error = ""
        app.pending_capture = {"text": "x", "point": (1, 2), "revision": 1}

        app._apply_highlight("yellow")          # 不许抛

        self.assertTrue(app.status_label.configure.called)

    def test_background_highlight_error_refloats_the_entry(self):
        # 后台失败时入口早收了，所以先把它在原来那次划词的位置重新浮出来
        app = word_picker.WordPickerApp.__new__(word_picker.WordPickerApp)
        app.status_label = mock.Mock()
        app.float_entry = mock.Mock()
        app._show_float_entry = mock.Mock(return_value=True)

        app._report_highlight_error("高光失败：原文已变化", (320, 240))

        app._show_float_entry.assert_called_once_with((320, 240))
        app.float_entry.flash.assert_called_once()
        self.assertIn("原文已变化", app.float_entry.flash.call_args.args[0])

        # 没有坐标时只剩状态行，也不能抛
        app._show_float_entry.reset_mock()
        app._report_highlight_error("高光失败：连接不上", None)
        app._show_float_entry.assert_not_called()

    def test_float_entry_flash_shows_a_message_then_collapses(self):
        if word_picker.FloatingEntry is None or word_picker.ui_icons is None:
            self.skipTest("front 入口层没加载")
        try:
            root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(str(exc))
        root.withdraw()
        scale = word_picker.ui_theme.detect_scale(root)
        icons = word_picker.ui_icons.IconCache(
            str(word_picker.ASSETS_UI_DIR / "icons"), scale
        )
        colors = word_picker.ui_theme.apply(
            root, palette=word_picker.ui_theme.default_palette(),
            scale=scale, icons=icons,
        )
        entry = word_picker.FloatingEntry(root, icons, scale=scale, colors=colors)
        try:
            entry.show(400, 300)
            root.update()
            entry.flash("高光只支持 Obsidian 本地 Markdown 与 PDF。", duration_ms=120)
            root.update()
            self.assertTrue(entry.strip.winfo_manager(), "提示没有显示出来")
            self.assertEqual(len(entry.strip.winfo_children()), 1)

            time.sleep(0.3)
            root.update()
            self.assertFalse(
                entry.strip.winfo_manager(), "提示到点之后应当自己收起"
            )
        finally:
            entry.destroy()
            root.destroy()

    # ------------------------------------------------------------ PDF 高光
    @staticmethod
    def _wait_for(predicate, timeout=5.0):
        """等一个后台线程的结果。桩是立即返回的，所以这里只是把调度让出来。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return True
            time.sleep(0.02)
        return predicate()

    def _highlight_detection_app(self, vault, captured):
        app = word_picker.WordPickerApp.__new__(word_picker.WordPickerApp)
        app.events = mock.Mock()
        app.highlight_source_revision = -1
        app.highlight_source_context = None
        app.highlight_source_error = ""
        app.config = mock.Mock(values={"obsidian_vault_path": str(vault)})
        client = mock.Mock()
        client.capture.return_value = captured
        return app, client

    def test_highlight_reset_requires_confirmation_and_sends_expected_count(self):
        client = word_picker.ObsidianBridgeClient.__new__(word_picker.ObsidianBridgeClient)
        client._request = mock.Mock(side_effect=[{"active_highlights": 92}, {"removed": 92}])
        self.assertEqual(client.highlight_diagnostics()["active_highlights"], 92)
        self.assertEqual(client.reset_highlights(92)["removed"], 92)
        self.assertEqual(client._request.call_args_list[0].args, ("/highlight-diagnostics",))
        self.assertEqual(client._request.call_args_list[1].args,
                         ("/highlight-reset", {
                             "confirm": "CLEAR_ALL_HIGHLIGHTS", "expected_count": 92,
                         }))

    def test_highlight_reset_ui_never_deletes_before_user_confirms(self):
        app = word_picker.WordPickerApp.__new__(word_picker.WordPickerApp)
        app.obsidian_vault_var = mock.Mock()
        app.obsidian_vault_var.get.return_value = "C:/OtherQuestionVault"
        app.config = types.SimpleNamespace(values={"highlight_vault_path": "C:/Vault"})
        app.events = mock.Mock()
        app.root = mock.Mock()
        app._set_transient_status = mock.Mock()
        bridge = mock.Mock()
        bridge.highlight_diagnostics.return_value = {"active_highlights": 12}
        bridge.reset_highlights.return_value = {"removed": 12}
        def immediate_thread(*, target, **_kwargs):
            return types.SimpleNamespace(start=target)
        with mock.patch.object(word_picker, "ObsidianBridgeClient", return_value=bridge), \
             mock.patch.object(word_picker.threading, "Thread", side_effect=immediate_thread), \
             mock.patch.object(word_picker.messagebox, "askyesno", side_effect=[False, True]) as ask:
            app._clear_all_highlights()
            preview=app.events.put.call_args.args[0]
            self.assertEqual(preview, ("highlight_reset_preview", "C:/Vault", 12, ""))
            app._confirm_highlight_reset("C:/Vault", 12)
            bridge.reset_highlights.assert_not_called()
            app._confirm_highlight_reset("C:/Vault", 12)
            bridge.reset_highlights.assert_called_once_with(12)
            self.assertEqual(app.events.put.call_args.args[0][0], "highlight_reset_done")
            self.assertEqual(ask.call_count, 2)

    def test_pdf_is_accepted_as_a_highlight_source(self):
        # PDF 写不进正文，但**支持**：颜色与页码记进高光索引，PDF 本体一字不改
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary) / "vault"
            (vault / ".obsidian").mkdir(parents=True)
            pdf = word_picker.ObsidianFileContext(
                vault_path=str(vault), file_path="资料/讲义.pdf",
                file_name="讲义.pdf", extension="pdf", page_number=12,
            )
            app, client = self._highlight_detection_app(vault, pdf)
            with mock.patch.object(word_picker, "ObsidianBridgeClient",
                                   return_value=client):
                app._start_highlight_source_detection(
                    3, mock.Mock(process_name="Obsidian.exe")
                )
                self.assertTrue(self._wait_for(lambda: app.events.put.called))

            kind, revision, context, error = app.events.put.call_args.args[0]
            self.assertEqual(kind, "highlight_source_ready")
            self.assertEqual(revision, 3)
            self.assertIs(context, pdf)
            self.assertEqual(error, "")

    def test_non_pdf_vault_file_is_rejected_with_a_clear_reason(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary) / "vault"
            (vault / ".obsidian").mkdir(parents=True)
            docx = word_picker.ObsidianFileContext(
                vault_path=str(vault), file_path="资料/表格.docx",
                file_name="表格.docx", extension="docx",
            )
            app, client = self._highlight_detection_app(vault, docx)
            with mock.patch.object(word_picker, "ObsidianBridgeClient",
                                   return_value=client):
                app._start_highlight_source_detection(
                    4, mock.Mock(process_name="Obsidian.exe")
                )
                self.assertTrue(self._wait_for(lambda: app.events.put.called))

            _, _, context, error = app.events.put.call_args.args[0]
            self.assertIsNone(context)
            self.assertIn(".docx", error)

    def test_pdf_highlight_uses_plugin_store_without_writing_index(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary) / "vault"
            vault.mkdir()
            app = word_picker.WordPickerApp.__new__(word_picker.WordPickerApp)
            app.events = mock.Mock()
            pdf = word_picker.ObsidianFileContext(
                vault_path=str(vault), file_path="资料/讲义.pdf",
                file_name="讲义.pdf", extension="pdf", page_number=12,
                capture_id="capture-9",
            )
            with mock.patch.object(word_picker, "ObsidianBridgeClient") as client:
                client.return_value.apply_highlight.return_value = {
                    "file_name": "讲义.pdf", "highlight_id": "hl-pdf",
                }
                app._run_highlight(pdf, "yellow", "#e0ac00", "两行\n原文")
            client.return_value.apply_highlight.assert_called_once_with(
                "capture-9", "yellow"
            )
            self.assertFalse(vault.joinpath(*LEGACY_HIGHLIGHT_INDEX_PARTS).exists())
            self.assertEqual(app.events.put.call_args.args[0][0], "highlight_done")
            self.assertEqual(app.events.put.call_args.args[0][3], "hl-pdf")

    def test_pdf_bridge_failure_does_not_create_index_or_claim_success(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary) / "vault"
            vault.mkdir()
            app = word_picker.WordPickerApp.__new__(word_picker.WordPickerApp)
            app.events = mock.Mock()
            pdf = word_picker.ObsidianFileContext(
                vault_path=str(vault), file_path="资料/讲义.pdf",
                file_name="讲义.pdf", extension="pdf", page_number=7,
                capture_id="capture-3",
            )
            with mock.patch.object(word_picker, "ObsidianBridgeClient") as client:
                client.return_value.apply_highlight.side_effect = (
                    word_picker.ObsidianBridgeError("无法连接 Obsidian 桥接插件。")
                )
                app._run_highlight(pdf, "green", "#08b94e", "选中文字")
            self.assertFalse(vault.joinpath(*LEGACY_HIGHLIGHT_INDEX_PARTS).exists())
            event = app.events.put.call_args.args[0]
            self.assertEqual(event[0], "highlight_error")
            self.assertIn("无法连接", event[2])

    def test_markdown_highlight_does_not_touch_old_index(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary) / "vault"
            vault.mkdir()
            index = vault.joinpath(*LEGACY_HIGHLIGHT_INDEX_PARTS)
            index.parent.mkdir(parents=True)
            index.write_text("历史记录\n", encoding="utf-8")
            app = word_picker.WordPickerApp.__new__(word_picker.WordPickerApp)
            app.events = mock.Mock()
            context = word_picker.ObsidianCaptureContext(
                vault_path=str(vault), capture_id="capture-1",
                file_path="课程/第一章.md", file_name="第一章.md",
                selection="被划选的文本",
            )
            with mock.patch.object(word_picker, "ObsidianBridgeClient") as client:
                client.return_value.apply_highlight.return_value = {
                    "file_name": "第一章.md", "highlight_id": "hl-abc123",
                }
                app._run_highlight(context, "yellow", "#e0ac00", "被划选的文本")
            client.return_value.apply_highlight.assert_called_once_with(
                "capture-1", "yellow"
            )
            self.assertEqual(index.read_text(encoding="utf-8"), "历史记录\n")
            self.assertEqual(app.events.put.call_args.args[0][3], "hl-abc123")

    def test_markdown_success_does_not_use_legacy_index_writer(self):
        # 旧索引写入器（_append_highlight_index）已经被插件落盘取代、从程序里删掉了。
        # 这里仍然把它装成一个会抛错的假实现：将来谁又把它加回来并调用，这条会立刻红。
        app = word_picker.WordPickerApp.__new__(word_picker.WordPickerApp)
        app.events = mock.Mock()
        app._append_highlight_index = mock.Mock(side_effect=OSError("旧索引不可写"))
        context = word_picker.ObsidianCaptureContext(
            vault_path="C:/vault", capture_id="capture-2",
            file_path="课程/第一章.md", file_name="第一章.md", selection="重点",
        )
        with mock.patch.object(word_picker, "ObsidianBridgeClient") as client:
            client.return_value.apply_highlight.return_value = {
                "file_path": context.file_path, "file_name": context.file_name,
                "highlight_id": "hl-ok",
            }
            app._run_highlight(context, "yellow", "#e0ac00", "重点", (6, 7))
        self.assertEqual(app.events.put.call_args.args[0][0], "highlight_done")
        app._append_highlight_index.assert_not_called()

    def test_run_highlight_reports_bridge_failure_without_writing_index(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary) / "vault"
            vault.mkdir()
            app = word_picker.WordPickerApp.__new__(word_picker.WordPickerApp)
            app.config = mock.Mock(values={})
            app.events = mock.Mock()
            context = word_picker.ObsidianCaptureContext(
                vault_path=str(vault), capture_id="capture-1",
                file_path="课程/第一章.md", file_name="第一章.md",
                selection="被划选的文本",
            )

            with mock.patch.object(word_picker, "ObsidianBridgeClient") as client:
                client.return_value.apply_highlight.side_effect = (
                    word_picker.ObsidianBridgeError("原文已变化或存在多个相同片段，请重新划选。")
                )
                app._run_highlight(context, "yellow", "#e0ac00", "被划选的文本")

            # 来源文档没写成，索引里就不该留下一条指不到东西的记录
            index_path = vault.joinpath(*LEGACY_HIGHLIGHT_INDEX_PARTS)
            self.assertFalse(Path(index_path).exists())
            event = app.events.put.call_args.args[0]
            self.assertEqual(event[0], "highlight_error")
            self.assertIn("原文已变化", event[2])


class NativeModeBarTests(unittest.TestCase):
    def test_question_obsidian_md_pdf_selection_survives_stale_native_ownership(self):
        for extension in ("md", "pdf"):
            for pending, ready in ((True, False), (False, True), (True, True)):
                with self.subTest(extension=extension, pending=pending, ready=ready):
                    app = word_picker.WordPickerApp.__new__(word_picker.WordPickerApp)
                    app.monitoring = True
                    app.capture_mode = word_picker.MODE_QUESTION
                    app.capture_paused = False
                    app._native_highlight_sync_pending = pending
                    app._native_highlight_ready = ready
                    app.capture_revision = 3
                    app.pending_capture = {"text": "old"}
                    app.float_entry = mock.Mock()
                    app.capture = mock.Mock()
                    app.capture.clipboard_sequence.side_effect = [10, 11, 11]
                    app.capture.read_clipboard_text.return_value = "Obsidian 选区中的问题内容"
                    app.config = types.SimpleNamespace(values={
                        "obsidian_vault_path": "C:/QuestionVault",
                        "highlight_vault_path": "C:/DifferentHighlightVault",
                    })
                    app.events = queue.Queue()
                    app.root = mock.Mock()
                    app._offer_capture = mock.Mock()
                    bridge = mock.Mock()
                    context = word_picker.ObsidianCaptureContext(
                        "C:/QuestionVault", "cap", "a.md", "a.md", "Obsidian 选区中的问题内容"
                    ) if extension == "md" else word_picker.ObsidianFileContext(
                        "C:/QuestionVault", "a.pdf", "a.pdf", "pdf", 1, "cap", "Obsidian 选区中的问题内容"
                    )
                    bridge.capture.return_value = context
                    def thread(*, target, **kwargs):
                        return types.SimpleNamespace(start=target)
                    with mock.patch.object(word_picker, "ObsidianBridgeClient", return_value=bridge) as client, \
                         mock.patch.object(word_picker.threading, "Thread", side_effect=thread):
                        app._on_drag((40, 50), types.SimpleNamespace(process_name="Obsidian.exe"))
                    app.capture.send_copy.assert_not_called()
                    client.assert_called_once_with(Path("C:/QuestionVault"))
                    app._offer_capture.assert_not_called()
                    self.assertTrue(app.pending_capture["deferred"])
                    app._handle_record_selection_ready(4, context)
                    app._begin_question_record = mock.Mock()
                    app._on_float_entry_click(40, 50)
                    app._begin_question_record.assert_called_once_with("Obsidian 选区中的问题内容", (40, 50))
                    app.capture.send_copy.assert_not_called()
                    self.assertEqual(app.events.get_nowait(), ("source_context_ready", 4, context, ""))

    def test_translation_obsidian_selection_survives_stale_native_ownership(self):
        app = word_picker.WordPickerApp.__new__(word_picker.WordPickerApp)
        app.monitoring = True
        app.capture_mode = word_picker.MODE_TRANSLATE
        app.capture_paused = False
        app._native_highlight_sync_pending = True
        app._native_highlight_ready = True
        app.capture_revision = 3
        app.float_entry = mock.Mock()
        app.capture = mock.Mock()
        app.capture.clipboard_sequence.side_effect = [10, 11, 11]
        app.capture.read_clipboard_text.return_value = "Selected text"
        app.config = types.SimpleNamespace(values={"obsidian_vault_path": "C:/QuestionVault"})
        app.root = mock.Mock()
        app._offer_capture = mock.Mock()
        app._prepared_words_for_sentence = mock.Mock(return_value=[ITEM])
        app.events = queue.Queue()
        context = word_picker.ObsidianCaptureContext("C:/QuestionVault", "cap", "a.md", "a.md", "Selected text")
        def thread(*, target, **kwargs):
            return types.SimpleNamespace(start=target)
        with mock.patch.object(word_picker, "ObsidianBridgeClient") as bridge, mock.patch.object(word_picker.threading, "Thread", side_effect=thread):
            bridge.return_value.capture.return_value = context
            app._on_drag((40, 50), types.SimpleNamespace(process_name="Obsidian.exe"))
        app.capture.send_copy.assert_not_called()
        app._offer_capture.assert_not_called()
        self.assertEqual(app.events.get_nowait(), ("record_selection_ready", 4, context, ""))
        app._handle_record_selection_ready(4, context)
        app._begin_lookup = mock.Mock()
        app._on_float_entry_click(40, 50)
        app._begin_lookup.assert_called_once_with("Selected text", (40, 50), [ITEM])
        app.capture.send_copy.assert_not_called()

    def test_disabling_native_entry_and_retries_do_not_cancel_question_capture(self):
        for observed in (True, None):
            with self.subTest(observed=observed):
                app = word_picker.WordPickerApp.__new__(word_picker.WordPickerApp)
                app.capture_mode = word_picker.MODE_QUESTION
                app.config = types.SimpleNamespace(values={"highlight_vault_path": "C:/HighVault"})
                app._native_highlight_sync_revision = 0
                app._native_highlight_sync_lock = threading.Lock()
                app._native_highlight_ready = True
                app._native_highlight_sync_pending = True
                app.capture_revision = 9
                pending_question = {"revision": 9, "text": "正在记录的问题"}
                app.pending_capture = pending_question
                app._hide_float_entry = mock.Mock()
                app._set_transient_status = mock.Mock()
                app.root = mock.Mock()
                app.root.winfo_exists.return_value = False
                app.events = queue.Queue()
                bridge = mock.Mock()
                bridge.configure_highlight_entry.side_effect = word_picker.ObsidianBridgeTimeout("timeout")
                if observed is None:
                    bridge._request.side_effect = word_picker.ObsidianBridgeTimeout("timeout")
                else:
                    bridge._request.return_value = {"native_highlight_entry": observed}
                def thread(*, target, **kwargs):
                    return types.SimpleNamespace(start=target)
                with mock.patch.object(word_picker, "ObsidianBridgeClient", return_value=bridge), \
                     mock.patch.object(word_picker.threading, "Thread", side_effect=thread):
                    for attempt in range(2):
                        app._start_highlight_entry_sync()
                        app._process_events()
                        self.assertFalse(app._native_highlight_ready)
                        self.assertFalse(app._native_highlight_sync_pending)
                        self.assertIs(app.pending_capture, pending_question)
                        self.assertEqual(app.capture_revision, 9)
                app._hide_float_entry.assert_not_called()
                self.assertEqual(bridge.configure_highlight_entry.call_args_list, [mock.call(False)] * 2)

    def test_enabled_native_ack_in_question_mode_does_not_hide_question_entry(self):
        app = word_picker.WordPickerApp.__new__(word_picker.WordPickerApp)
        app.capture_mode = word_picker.MODE_QUESTION
        app._native_highlight_sync_revision = 2
        app._native_highlight_ready = True
        app._native_highlight_sync_pending = True
        app.capture_revision = 9
        pending = {"revision": 9, "text": "正在记录的问题"}
        app.pending_capture = pending
        app._hide_float_entry = mock.Mock()
        app.events = queue.Queue()
        app.events.put(("highlight_entry_sync", 2, True, ""))
        app.root = mock.Mock()
        app.root.winfo_exists.return_value = False
        app._process_events()
        self.assertFalse(app._native_highlight_ready)
        self.assertFalse(app._native_highlight_sync_pending)
        self.assertEqual(app.capture_revision, 9)
        self.assertIs(app.pending_capture, pending)
        app._hide_float_entry.assert_not_called()

    def test_native_sync_blocks_obsidian_capture_in_highlight_mode(self):
        app = word_picker.WordPickerApp.__new__(word_picker.WordPickerApp)
        app.monitoring = True
        app.capture_mode = word_picker.MODE_HIGHLIGHT
        app._native_highlight_sync_pending = True
        app.capture_revision = 3
        app.pending_capture = {"text": "old"}
        app.capture = mock.Mock()
        app.float_entry = mock.Mock()
        app._on_drag((40, 50), types.SimpleNamespace(process_name="Obsidian.exe"))
        app.capture.send_copy.assert_not_called()
        app.float_entry.hide.assert_called_once()
        self.assertIsNone(app.pending_capture)
        self.assertEqual(app.capture_revision, 4)

    def test_switch_mode_cancels_delayed_question_clipboard_result(self):
        app = word_picker.WordPickerApp.__new__(word_picker.WordPickerApp)
        app.capture_mode = word_picker.MODE_QUESTION
        app.capture_revision = 6
        app.pending_capture = {"text": "old"}
        app.mode_var = mock.Mock()
        app.mode_var.get.return_value = next(name for name, mode in word_picker.PAGE_LABELS.items()
                                             if mode == word_picker.MODE_HIGHLIGHT)
        app.page_frames = {}
        app.float_entry = mock.Mock()
        app._refresh_nav = mock.Mock()
        app._sync_status = mock.Mock()
        app._offer_capture = mock.Mock()
        app._on_mode_changed()
        app._finish_capture(0, (10, 10), 6)
        app._offer_capture.assert_not_called()
        self.assertIsNone(app.pending_capture)
        app.float_entry.hide.assert_called_once()

    def test_unknown_sync_result_does_not_enable_duplicate_fallback(self):
        app = word_picker.WordPickerApp.__new__(word_picker.WordPickerApp)
        app.capture_mode = word_picker.MODE_HIGHLIGHT
        app.events = word_picker.queue.Queue()
        app.events.put(("highlight_entry_sync", 2, None, "timeout"))
        app._native_highlight_sync_revision = 2
        app._native_highlight_ready = True
        app.float_entry = mock.Mock()
        app.root = mock.Mock()
        app.root.winfo_exists.return_value = False
        app._set_transient_status = mock.Mock()
        app._process_events()
        self.assertTrue(app._native_highlight_sync_pending)
        self.assertTrue(app._native_highlight_ready)
        app.root.after.assert_called_once()

    def test_plugin_mode_event_dispatches_on_tk_and_updates_entry(self):
        app = word_picker.WordPickerApp.__new__(word_picker.WordPickerApp)
        app.events = mock.Mock()
        app.events.get_nowait.side_effect = [
            ("native_mode_switch", word_picker.MODE_QUESTION),
            __import__("queue").Empty,
        ]
        # _process_events may schedule itself again; use Tk-compatible stub.
        app.root = mock.Mock()
        app._switch_capture_mode = mock.Mock()
        app._process_events()
        app._switch_capture_mode.assert_called_once_with(word_picker.MODE_QUESTION)

    def test_float_mode_bar_has_three_click_targets_and_stays_on_screen(self):
        try:
            root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(str(exc))
        root.withdraw()
        from front.float_entry import FloatingEntry
        selected = []
        entry = FloatingEntry(root, None, colors={
            "bg_primary": "#202020", "bg_hover": "#2c2c2c", "bg_active": "#343434",
            "border": "#555555", "accent": "#a381ff", "accent_active": "#8f6ff0",
            "text_normal": "#eeeeee", "text_muted": "#bbbbbb", "text_on_accent": "#ffffff",
        }, mode_options=[("question", "记录问题", "pencil"),
                         ("highlight", "高光", "palette"),
                         ("translate", "记录单词", "feather")],
            active_mode="highlight", on_mode_select=selected.append)
        try:
            entry.show(root.winfo_screenwidth() - 4, root.winfo_screenheight() - 4)
            root.update_idletasks()
            self.assertEqual(len(entry._mode_cells), 3)
            self.assertIsNotNone(entry.mode_bar)
            self.assertGreater(entry.mode_bar.winfo_y(), entry._row.winfo_y())
            self.assertGreaterEqual(entry.window.winfo_x(), 0)
            self.assertGreaterEqual(entry.window.winfo_y(), 0)
            self.assertLessEqual(entry.window.winfo_x() + entry.window.winfo_width(),
                                 root.winfo_screenwidth())
            self.assertLessEqual(entry.window.winfo_y() + entry.window.winfo_height(),
                                 root.winfo_screenheight())
            # 选中态是药丸底、未选中是平的；悬停只提亮未选中项，选中的药丸要留住
            self.assertEqual(entry._mode_cells["highlight"].cget("background"), "#343434")
            self.assertEqual(entry._mode_cells["question"].cget("background"), "#202020")
            entry._hover_mode("question", True)
            self.assertEqual(entry._mode_cells["question"].cget("background"), "#2c2c2c")
            entry._hover_mode("highlight", True)
            self.assertEqual(entry._mode_cells["highlight"].cget("background"), "#343434",
                             "选中的药丸不能被悬停刷掉")
            entry._hover_mode("question", False)
            self.assertEqual(entry._mode_cells["question"].cget("background"), "#202020")
            entry._mode_cells["question"].event_generate("<Button-1>")
            root.update()
            self.assertEqual(selected, ["question"])
            entry.set_active_mode("question")
            self.assertEqual(entry.active_mode, "question")
            # 悬停整条入口时主按钮不能跟着变灰：它是整块强调色的按钮
            entry._apply_entry_background()
            self.assertEqual(entry.icon_label.cget("background"), "#a381ff")
            for mode, label in (("question", "记录问题"), ("translate", "记录单词")):
                entry.set_active_mode(mode)
                entry.show(root.winfo_screenwidth() - 4, root.winfo_screenheight() - 4)
                root.update_idletasks()
                self.assertEqual(entry.icon_label.cget("text"), label)
                self.assertEqual(str(entry.icon_label.cget("image")), "")
                # 记录模式的主按钮是强调色实心块，文字用反色
                self.assertEqual(entry.icon_label.cget("background"), "#a381ff")
                self.assertEqual(entry.icon_label.cget("foreground"), "#ffffff")
                self.assertGreaterEqual(entry.window.winfo_width(), 150)
                self.assertLessEqual(entry.window.winfo_x() + entry.window.winfo_width(), root.winfo_screenwidth())
                self.assertLessEqual(entry.window.winfo_y() + entry.window.winfo_height(), root.winfo_screenheight())
            # 切回高光：主按钮回到只有一个图标，底色也得还原，不能留着上一模式的强调色
            entry.set_active_mode("highlight")
            entry.show(root.winfo_screenwidth() - 4, root.winfo_screenheight() - 4)
            root.update_idletasks()
            self.assertEqual(entry.icon_label.cget("background"), "#202020")
            self.assertLessEqual(entry.window.winfo_x() + entry.window.winfo_width(), root.winfo_screenwidth())
        finally:
            entry.destroy()
            root.destroy()

    def test_mode_page_change_updates_visible_bar_active_state(self):
        app = word_picker.WordPickerApp.__new__(word_picker.WordPickerApp)
        app.mode_var = mock.Mock()
        app.mode_var.get.return_value = next(name for name, mode in word_picker.PAGE_LABELS.items()
                                              if mode == word_picker.MODE_HIGHLIGHT)
        app.page_frames = {word_picker.MODE_HIGHLIGHT: mock.Mock()}
        app.float_entry = mock.Mock()
        app._refresh_nav = mock.Mock()
        app._sync_status = mock.Mock()
        app._on_mode_changed()
        app.float_entry.set_active_mode.assert_called_once_with(word_picker.MODE_HIGHLIGHT)


class DeferredCaptureTests(unittest.TestCase):
    def app(self, mode=word_picker.MODE_QUESTION):
        app = word_picker.WordPickerApp.__new__(word_picker.WordPickerApp)
        app.monitoring = True
        app.capture_mode = mode
        app.capture_revision = 3
        app.capture_paused = False
        app.pending_capture = None
        app.root = mock.Mock()
        app.capture = mock.Mock()
        app.capture.last_pointer_down = None
        app.capture.clipboard_sequence.return_value = 10
        app.capture.read_clipboard_text.return_value = "    if ready:\n\t    run('a  b')"
        app.float_entry = mock.Mock()
        app._show_float_entry = mock.Mock(return_value=True)
        app._set_transient_status = mock.Mock()
        app._begin_question_record = mock.Mock()
        app._begin_lookup = mock.Mock()
        app._prepared_words_for_sentence = mock.Mock(return_value=[ITEM])
        app.config = types.SimpleNamespace(values={
            "minimum_text_length": 2, "obsidian_vault_path": "C:/QuestionVault",
            "highlight_vault_path": "C:/HighlightVault",
        })
        app.events = queue.Queue()
        return app

    def drag(self, app, process="editor.exe"):
        window = types.SimpleNamespace(process_name=process, handle=123)
        with mock.patch.object(word_picker.threading, "Thread"):
            app._on_drag((40, 50), window)
        return window

    def test_all_modes_drag_and_hover_leave_clipboard_untouched(self):
        for mode in (word_picker.MODE_QUESTION, word_picker.MODE_TRANSLATE, word_picker.MODE_HIGHLIGHT):
            with self.subTest(mode=mode):
                app = self.app(mode)
                app._highlight_entry_swatches = mock.Mock(return_value=[])
                self.drag(app, "Obsidian.exe")
                app._on_float_entry_hover()
                app.capture.send_copy.assert_not_called()
                app.capture.clipboard_sequence.assert_not_called()
                app.capture.read_clipboard_text.assert_not_called()
                app._begin_question_record.assert_not_called()
                app._begin_lookup.assert_not_called()
                self.assertEqual(app.pending_capture["text"], "")

    def test_external_record_click_copies_once_then_opens_without_second_click(self):
        for mode in (word_picker.MODE_QUESTION, word_picker.MODE_TRANSLATE):
            with self.subTest(mode=mode):
                app = self.app(mode)
                self.drag(app)
                app.capture.clipboard_sequence.side_effect = [10, 11, 11]
                order = []
                app.capture.focus_source_window.side_effect = lambda handle: order.append(("focus", handle))
                app.capture.send_copy.side_effect = lambda: order.append(("copy",))
                app._on_float_entry_click(40, 50)
                self.assertEqual(order, [("focus", 123), ("copy",)])
                app.capture.send_copy.assert_called_once()
                if mode == word_picker.MODE_QUESTION:
                    app._begin_question_record.assert_called_once_with("    if ready:\n\t    run('a  b')", (40, 50))
                else:
                    app._begin_lookup.assert_called_once_with("if ready: run('a b')", (40, 50), [ITEM])
                self.assertIsNone(app.pending_capture)

    def test_repeated_record_click_does_not_send_multiple_copies(self):
        app = self.app()
        self.drag(app)
        app._on_float_entry_click(40, 50)
        app._on_float_entry_click(40, 50)
        app.capture.send_copy.assert_called_once()
        self.assertTrue(app.pending_capture["copying"])
        app._begin_question_record.assert_not_called()

    def test_bridge_snapshot_opens_md_pdf_and_word_pages_without_copying(self):
        for mode in (word_picker.MODE_QUESTION, word_picker.MODE_TRANSLATE):
            for extension in ("md", "pdf"):
                with self.subTest(mode=mode, extension=extension):
                    app = self.app(mode)
                    self.drag(app, "Obsidian.exe")
                    context = word_picker.ObsidianCaptureContext("C:/QuestionVault", "cap", "a.md", "a.md", "Selected text") if extension == "md" else word_picker.ObsidianFileContext("C:/QuestionVault", "a.pdf", "a.pdf", "pdf", 2, "cap", "Selected text")
                    app._handle_record_selection_ready(4, context)
                    app._on_float_entry_click(40, 50)
                    app.capture.send_copy.assert_not_called()
                    app.capture.clipboard_sequence.assert_not_called()
                    app.capture.read_clipboard_text.assert_not_called()
                    opener = app._begin_question_record if mode == word_picker.MODE_QUESTION else app._begin_lookup
                    self.assertEqual(opener.call_count, 1)

    def test_record_click_waits_for_inflight_bridge_then_opens_once(self):
        app = self.app()
        self.drag(app, "Obsidian.exe")
        app._on_float_entry_click(40, 50)
        app.capture.send_copy.assert_not_called()
        app._begin_question_record.assert_not_called()
        context = word_picker.ObsidianCaptureContext("C:/QuestionVault", "cap", "a.md", "a.md", "Selected text")
        app._handle_record_selection_ready(4, context)
        app._handle_record_selection_ready(4, context)
        app._begin_question_record.assert_called_once_with("Selected text", (40, 50))
        app.capture.send_copy.assert_not_called()

    def test_failed_bridge_falls_back_to_copy_only_after_record_click(self):
        app = self.app()
        self.drag(app, "Obsidian.exe")
        app.source_detection_pending = False
        app._handle_record_selection_ready(4, None, "offline")
        app.capture.send_copy.assert_not_called()
        app._on_float_entry_click(40, 50)
        app.capture.send_copy.assert_called_once()

    def test_click_away_cancels_waiting_bridge_and_prevents_late_copy(self):
        app = self.app()
        self.drag(app, "Obsidian.exe")
        app._on_float_entry_click(40, 50)
        app.float_entry.is_visible.return_value = True
        app.float_entry.contains.return_value = False
        app._on_pointer_down((900, 900))
        app._handle_record_selection_ready(4, None, "offline")
        app.capture.send_copy.assert_not_called()
        self.assertIsNone(app.pending_capture)

    def test_new_drag_cancels_old_copy_poll_result(self):
        app = self.app()
        self.drag(app)
        app._on_float_entry_click(40, 50)
        callback = app.root.after.call_args.args[1]
        self.drag(app)
        callback()
        app.capture.read_clipboard_text.assert_not_called()
        app._begin_question_record.assert_not_called()
        self.assertEqual(app.pending_capture["revision"], 5)
        self.assertFalse(app.pending_capture["requested"])

    def test_copy_timeout_can_be_retried_without_using_old_clipboard(self):
        app = self.app()
        self.drag(app)
        app._on_float_entry_click(40, 50)
        app._poll_clipboard(10, (40, 50), 4, word_picker.CLIPBOARD_POLL_TIMEOUT_MS)
        app.capture.read_clipboard_text.assert_not_called()
        self.assertFalse(app.pending_capture["requested"])
        app._on_float_entry_click(40, 50)
        self.assertEqual(app.capture.send_copy.call_count, 2)

    def test_closed_source_window_does_not_copy_or_open(self):
        app = self.app()
        self.drag(app)
        app.capture.focus_source_window.side_effect = word_picker.AppError("原划词窗口已关闭")
        app._on_float_entry_click(40, 50)
        app.capture.send_copy.assert_not_called()
        app._begin_question_record.assert_not_called()
        self.assertFalse(app.pending_capture["requested"])

    def test_highlight_uses_bridge_text_and_color_without_clipboard(self):
        for extension in ("md", "pdf"):
            with self.subTest(extension=extension):
                app = self.app(word_picker.MODE_HIGHLIGHT)
                self.drag(app, "Obsidian.exe")
                app._on_float_entry_pick("yellow")
                app.capture.send_copy.assert_not_called()
                context = word_picker.ObsidianCaptureContext("C:/HighlightVault", "cap", "a.md", "a.md", "    code") if extension == "md" else word_picker.ObsidianFileContext("C:/HighlightVault", "a.pdf", "a.pdf", "pdf", 2, "cap", "    code")
                with mock.patch.object(word_picker.threading, "Thread") as worker:
                    app._handle_highlight_source_ready(4, context)
                self.assertEqual(worker.call_args.kwargs["args"], (context, "yellow", "yellow", "    code", (40, 50)))
                app.capture.clipboard_sequence.assert_not_called()
                app.capture.read_clipboard_text.assert_not_called()
                app.capture.send_copy.assert_not_called()

    def test_bridge_pdf_capture_retains_selection_for_record_page(self):
        client = word_picker.ObsidianBridgeClient.__new__(word_picker.ObsidianBridgeClient)
        client.vault_path = Path("C:/vault")
        client._request = mock.Mock(return_value={
            "kind": "obsidian-file", "file_path": "a.pdf", "file_name": "a.pdf",
            "extension": "pdf", "page_number": 2, "capture_id": "cap",
            "highlight": {"exact": "    original code", "page_number": 2},
        })
        self.assertEqual(client.capture().selection, "    original code")

    def test_highlight_clipboard_completion_never_reads_clipboard(self):
        app = self.app(word_picker.MODE_HIGHLIGHT)
        app._finish_capture(1, (40, 50), 3)
        app.capture.clipboard_sequence.assert_not_called()
        app.capture.read_clipboard_text.assert_not_called()

    def test_record_button_stops_tk_default_focus_processing(self):
        entry = word_picker.FloatingEntry.__new__(word_picker.FloatingEntry)
        entry.on_click = mock.Mock()
        entry._pointer = (40, 50)
        self.assertEqual(entry._on_icon_click(), "break")
        entry.on_click.assert_called_once_with(40, 50)

    def test_source_window_focus_failure_does_not_send_copy(self):
        with mock.patch.object(word_picker.ctypes, "windll", create=True) as dll:
            user32 = dll.user32
            user32.IsWindow.return_value = True
            user32.GetForegroundWindow.return_value = 999
            with self.assertRaises(word_picker.AppError):
                word_picker.WindowsSelectionCapture.focus_source_window(123)
            user32.SetForegroundWindow.assert_called_once_with(123)


if __name__ == "__main__":
    unittest.main()
