"""Question popup layout tests; only temporary Tk widgets, no vault or user data."""
from pathlib import Path
import sys
import tkinter as tk
import types
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import word_picker as w


class QuestionSplitLayoutTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(str(exc))
        self.root.withdraw()
        app = w.WordPickerApp.__new__(w.WordPickerApp)
        self.app = app
        app.root = self.root
        app.ui_scale = 1.0
        app.ui_colors = None
        app.lookup_popup = None
        app.config = types.SimpleNamespace(values={})
        app.question_source_default_var = None
        app.question_supplement_default_var = None
        app.question_title_level_default_var = None
        app.question_obsidian_source_link_var = None
        app._defer_pending_obsidian_source = mock.Mock()
        with mock.patch.object(w, 'POPUP_CHARACTER_DIRS', []):
            app._create_question_popup('选中的原文\n第二行', (20, 20))
        self.root.update()

    def tearDown(self):
        if hasattr(self, 'root'):
            self.root.destroy()

    def test_split_is_vertical_and_editors_keep_existing_bindings(self):
        app = self.app
        split = app.question_editor_panes
        self.assertIsInstance(split, tk.PanedWindow)
        self.assertEqual(str(split.cget('orient')), 'vertical')
        self.assertTrue(split.cget('opaqueresize'))
        self.assertEqual(len(split.panes()), 2)
        self.assertEqual(app.question_selected_text_input.get('1.0', 'end-1c'), '选中的原文\n第二行')
        self.assertEqual(int(app.question_supplement_input.cget('height')), 6)
        self.assertTrue(app.question_supplement_input.bind('<Control-v>'))
        self.assertTrue(app.question_supplement_input.bind('<FocusIn>'))
        self.assertTrue(app.question_supplement_input.bind('<ButtonRelease-1>'))
        self.assertTrue(app.question_supplement_input.cget('yscrollcommand'))
        self.assertTrue(app.question_selected_text_input.cget('yscrollcommand'))

    def test_dragging_sash_expands_supplement_without_losing_text_or_toolbar(self):
        app = self.app
        popup = app.lookup_popup
        split = app.question_editor_panes
        app.question_supplement_input.insert('1.0', '补充第一段\n补充第二段')
        before_height = app.question_supplement_input.winfo_height()
        source_height = app.question_selected_text_input.winfo_height()
        toolbar_y = app.question_save_button.winfo_rooty()
        x, y = split.sash_coord(0)
        # Hit the real sash and run Tk's bound pointer-drag handlers.
        split.event_generate('<ButtonPress-1>', x=split.winfo_width()//2, y=y+2)
        split.event_generate('<B1-Motion>', x=split.winfo_width()//2, y=y+62)
        split.event_generate('<ButtonRelease-1>', x=split.winfo_width()//2, y=y+62)
        self.root.update()
        self.assertGreater(app.question_supplement_input.winfo_height(), before_height+25)
        self.assertLess(app.question_selected_text_input.winfo_height(), source_height)
        self.assertEqual(app.question_supplement_input.get('1.0', 'end-1c'), '补充第一段\n补充第二段')
        self.assertEqual(app.question_selected_text_input.get('1.0', 'end-1c'), '选中的原文\n第二行')
        self.assertEqual(app.question_save_button.winfo_rooty(), toolbar_y)
        self.assertLess(app.question_save_button.winfo_rooty()+app.question_save_button.winfo_height(),
                        popup.winfo_rooty()+popup.winfo_height())
        # Extreme drag cannot collapse either editor below its usable minimum.
        split.sash_place(0, x, split.winfo_height())
        self.root.update()
        self.assertGreaterEqual(app.question_selected_text_input.winfo_height(), 50)

    def test_drag_events_are_coalesced_and_final_position_is_not_lost(self):
        split = self.app.question_editor_panes
        self.assertIsInstance(split, w.SmoothVerticalPanedWindow)
        self.assertNotIn(split.winfo_class(), split.bindtags(), 'native proxy/drag cannot run too')
        self.assertFalse(split.cget('showhandle'))
        self.assertEqual(split.cget('background'), self.app._token('bg_primary'))
        x = split.winfo_width()//2
        _, y = split.sash_coord(0)
        split.event_generate('<ButtonPress-1>', x=x, y=y+2)
        with mock.patch.object(split, 'sash_place', wraps=split.sash_place) as place:
            for delta in range(1, 41):
                split.event_generate('<B1-Motion>', x=x, y=y+2+delta)
            self.assertEqual(place.call_count, 0, 'mouse bursts must not each trigger a layout')
            self.assertIsNotNone(split._frame_job)
            finished = tk.BooleanVar(self.root, False)
            self.root.after(35, lambda: finished.set(True))
            self.root.wait_variable(finished)
            self.assertEqual(place.call_count, 1, 'one frame applies only the most recent target')
            self.assertEqual(split.sash_coord(0)[1], y+40)
            # Last pointer arrives after that frame but before another frame is due.
            split.event_generate('<B1-Motion>', x=x, y=y+47)
            split.event_generate('<ButtonRelease-1>', x=x, y=y+47)
            self.root.update()
            self.assertEqual(place.call_count, 2)
            self.assertEqual(split.sash_coord(0)[1], y+45)
            self.assertIsNone(split._frame_job)
            self.assertIsNone(split._drag_anchor)
            self.assertEqual(split._grip.cget('background'), self.app._token('border'))
            self.assertIsNone(split.grab_current())

    def test_clicking_drag_handle_resizes_and_destroy_cancels_pending_frames(self):
        split = self.app.question_editor_panes
        grip = split._grip
        before = split.sash_coord(0)[1]
        grip.event_generate('<ButtonPress-1>', x=3, y=1)
        grip.event_generate('<B1-Motion>', x=3, y=21)
        grip.event_generate('<ButtonRelease-1>', x=3, y=21)
        self.root.update()
        self.assertEqual(split.sash_coord(0)[1], before+20)
        x = split.winfo_width()//2
        _, y = split.sash_coord(0)
        split.event_generate('<ButtonPress-1>', x=x, y=y+2)
        split.event_generate('<B1-Motion>', x=x, y=y+12)
        job = split._frame_job
        self.assertIsNotNone(job)
        self.app.lookup_popup.destroy()
        self.root.update()
        self.assertIsNone(split._frame_job)
        self.assertNotIn(job, self.root.tk.call('after', 'info'))
        self.assertIsNone(self.root.grab_current())

    def test_drag_capture_belongs_to_leaf_handle_not_editable_container(self):
        split = self.app.question_editor_panes
        grip = split._grip
        for press_widget in (split, grip):
            with self.subTest(press_widget=press_widget):
                _, sash_y = split.sash_coord(0)
                if press_widget is split:
                    px, py = split.winfo_width()//2, sash_y+2
                else:
                    px, py = 3, 1
                press_widget.event_generate('<ButtonPress-1>', x=px, y=py)
                self.assertIs(split.grab_current(), grip,
                              'grab on parent still dispatches mouse motion to Text descendants')
                self.assertEqual(grip.winfo_children(), [], 'capture owner must have no editor descendants')
                press_widget.event_generate('<ButtonRelease-1>', x=px, y=py)
                self.root.update()
                self.assertIsNone(split.grab_current())

    def test_capture_survives_pointer_leaving_sash_and_reversing_over_editor(self):
        split = self.app.question_editor_panes
        grip = split._grip
        self.app.question_supplement_input.insert('1.0', '不要变成选中文字')
        x = split.winfo_width()//2
        _, y = split.sash_coord(0)
        split.event_generate('<ButtonPress-1>', x=x, y=y+2)
        start_root_y = split._drag_anchor[0]
        # Grabbed events may be hundreds of pixels outside the tiny handle.
        # use root coordinates because moving the sash also moves the handle.
        for delta in (30, -15, 25):
            grip.event_generate('<B1-Motion>', x=-200, y=100,
                                rootx=split.winfo_rootx()-200, rooty=start_root_y+delta)
            finished = tk.BooleanVar(self.root, False)
            self.root.after(25, lambda: finished.set(True))
            self.root.wait_variable(finished)
            self.assertIs(split.grab_current(), grip)
            self.assertIsNotNone(split._drag_anchor)
            self.assertEqual(split.sash_coord(0)[1], y+delta)
        grip.event_generate('<ButtonRelease-1>', x=-200, y=100,
                            rootx=split.winfo_rootx()-200, rooty=start_root_y+20)
        self.root.update()
        self.assertEqual(split.sash_coord(0)[1], y+20)
        self.assertIsNone(split.grab_current())
        self.assertIsNone(split._drag_anchor)
        self.assertEqual(self.app.question_supplement_input.tag_ranges('sel'), ())

    def test_release_restores_existing_modal_grab(self):
        split = self.app.question_editor_panes
        popup = self.app.lookup_popup
        popup.grab_set()
        _, y = split.sash_coord(0)
        split.event_generate('<ButtonPress-1>', x=split.winfo_width()//2, y=y+2)
        self.assertIs(split.grab_current(), split._grip)
        split._grip.event_generate('<ButtonRelease-1>', x=3, y=1)
        self.root.update()
        self.assertIs(popup.grab_current(), popup)
        popup.grab_release()

    def test_window_resize_gives_space_to_editors_and_keeps_toolbar_fixed(self):
        app = self.app
        popup = app.lookup_popup
        before = app.question_editor_panes.winfo_height()
        popup.geometry(f'{popup.winfo_width()+80}x{popup.winfo_height()+100}')
        self.root.update()
        self.assertGreater(app.question_editor_panes.winfo_height(), before+70)
        self.assertGreater(app.question_supplement_input.winfo_width(), 600)
        self.assertLess(app.question_save_button.winfo_rooty()+app.question_save_button.winfo_height(),
                        popup.winfo_rooty()+popup.winfo_height())


if __name__ == '__main__':
    unittest.main()
