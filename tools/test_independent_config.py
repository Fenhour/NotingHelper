from copy import deepcopy
import json
import os
from pathlib import Path
import queue
import sys
import tempfile
import threading
import tkinter as tk
import types
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import word_picker as w


def immediate_thread(*, target, **kwargs):
    return types.SimpleNamespace(start=target)


class IndependentConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='助手 双仓库测试 ')
        self.root = Path(self.temp.name)
        self.a = self.root / '问题来源'
        self.b = self.root / '高光来源'
        self.c = self.root / '导出仓库'
        for vault in (self.a, self.b, self.c):
            (vault / '.obsidian').mkdir(parents=True)
        self.interpreter = tk.Tcl()

    def tearDown(self):
        self.temp.cleanup()

    def app(self):
        app = w.WordPickerApp.__new__(w.WordPickerApp)
        app.config = mock.Mock(values=deepcopy(w.AppConfig.DEFAULTS))
        app.config.values.update(obsidian_vault_path=str(self.a), highlight_vault_path=str(self.b),
                                 question_output_dir=str(self.a/'索引'), question_body_dir=str(self.a/'正文'))
        for attr, value in (
            ('highlight_vault_var',str(self.b)), ('obsidian_vault_var',str(self.a)),
            ('highlight_export_dir_var',str(self.c/'全部高光')),
            ('highlight_single_index_dir_var',str(self.c/'索引')),
            ('highlight_single_index_filename_var','摘录.md'),
            ('highlight_single_note_var',str(self.c/'注释')),
        ):
            setattr(app, attr, tk.StringVar(self.interpreter, value))
        app.highlight_active_colors = ['blue','yellow','green']
        app.highlight_native_entry_var = tk.BooleanVar(self.interpreter, True)
        for name in ('_refresh_highlight_color_buttons','_refresh_highlight_single_index_history',
                     '_refresh_highlight_export_button','_set_highlight_status','_set_transient_status',
                     '_start_highlight_entry_sync','_start_highlight_single_export_sync',
                     '_start_highlight_active_colors_sync','_hide_float_entry'):
            setattr(app,name,mock.Mock())
        app.events = queue.Queue()
        app._highlight_single_export_lock = threading.Lock()
        app._highlight_single_export_synced = None
        app._highlight_active_colors_lock = threading.Lock()
        app._highlight_active_colors_synced = None
        return app

    def test_legacy_vault_migrates_once_and_blank_stays_blank(self):
        p = self.root/'config.json'
        p.write_text(json.dumps({'obsidian_vault_path':str(self.a)}),encoding='utf-8')
        config = w.AppConfig(p)
        self.assertEqual(w.configured_highlight_vault(config.values), str(self.a))
        config.values['obsidian_vault_path']=str(self.b)
        config.save()
        self.assertEqual(w.configured_highlight_vault(w.AppConfig(p).values),str(self.a))
        config.values['highlight_vault_path']=''
        config.save()
        self.assertEqual(w.configured_highlight_vault(w.AppConfig(p).values),'')

    def test_default_export_root_does_not_follow_questions(self):
        values={'obsidian_vault_path':str(self.a), 'highlight_vault_path':str(self.b)}
        self.assertTrue(w.resolve_highlight_export_dir(values).is_relative_to(self.b))
        values['obsidian_vault_path']=str(self.c)
        self.assertTrue(w.resolve_highlight_export_dir(values).is_relative_to(self.b))
        values['highlight_vault_path']=''
        self.assertEqual(w.highlight_root(values),w.APP_DIR)

    def test_two_export_modes_resolve_destination_not_source(self):
        single=w.highlight_single_export_target(str(self.b),str(self.c/'索引/新.md'),str(self.c/'正文'))
        self.assertEqual(single,{'index_path':'索引/新.md','note_dir':'正文','vault_path':str(self.c)})
        batch=w.highlight_export_target(str(self.b),str(self.c/'批量'))
        self.assertEqual(batch,{'directory':'批量','vault_path':str(self.c)})
        self.assertNotIn('vault_path',w.highlight_export_target(str(self.b),str(self.b/'批量')))

    def test_mixed_destination_vaults_and_non_vault_paths_rejected(self):
        with self.assertRaises(w.ObsidianBridgeError):
            w.highlight_single_export_target(str(self.b),str(self.c/'索引.md'),str(self.a/'正文'))
        with self.assertRaises(w.ObsidianBridgeError):
            w.highlight_export_target(str(self.b),str(self.root/'没有仓库'))

    def test_highlight_save_works_without_question_or_api_controls(self):
        app=self.app(); questions={k:deepcopy(v) for k,v in app.config.values.items() if k.startswith('question_') or k=='obsidian_vault_path'}
        self.assertTrue(app._save_highlight_settings())
        for key,value in questions.items(): self.assertEqual(app.config.values[key],value)
        self.assertEqual(app._highlight_single_export_payload()['vault_path'],str(self.c))
        app.config.save.assert_called_once()
        app._start_highlight_single_export_sync.assert_called_once_with(notify=True)

    def test_invalid_visible_vault_cannot_save_into_old_vault(self):
        app=self.app(); before=deepcopy(app.config.values)
        app.highlight_vault_var.set(str(self.root/'不是仓库'))
        self.assertFalse(app._save_highlight_settings())
        self.assertEqual(app.config.values,before)
        app.config.save.assert_not_called()

    def test_highlight_save_failure_rolls_back(self):
        app=self.app(); before=deepcopy(app.config.values)
        app.config.save.side_effect=OSError('只读')
        self.assertFalse(app._save_highlight_settings())
        self.assertEqual(app.config.values,before)
        app._start_highlight_entry_sync.assert_not_called()

    def test_highlight_switch_remembers_targets_colors_and_never_changes_questions(self):
        app=self.app(); questions={k:deepcopy(v) for k,v in app.config.values.items() if k.startswith('question_') or k=='obsidian_vault_path'}
        app.highlight_source_revision=12
        app.highlight_vault_var.set(str(self.c)); app._on_highlight_vault_changed()
        self.assertEqual(app.config.values['highlight_single_index_path'],'')
        self.assertEqual(app.config.values['highlight_export_dir'],'')
        self.assertTrue(Path(app.highlight_export_dir_var.get()).is_relative_to(self.c))
        self.assertEqual(app.highlight_source_revision,-1)
        app._start_highlight_entry_sync.assert_called_with(previous_vault=str(self.b))
        app.highlight_vault_var.set(str(self.b)); app._on_highlight_vault_changed()
        self.assertEqual(app.highlight_single_index_filename_var.get(),'摘录.md')
        self.assertEqual(app.highlight_single_note_var.get(),str(self.c/'注释'))
        self.assertEqual(app.highlight_export_dir_var.get(),str(self.c/'全部高光'))
        self.assertEqual(app.highlight_active_colors,['blue','yellow','green'])
        for key,value in questions.items(): self.assertEqual(app.config.values[key],value)

    def test_invalid_vault_switch_does_not_touch_either_domain(self):
        app=self.app(); before=deepcopy(app.config.values)
        app.highlight_vault_var.set(str(self.root/'缺少设置'))
        app._on_highlight_vault_changed()
        self.assertEqual(app.config.values,before)
        app.config.save.assert_not_called()

    def test_switch_save_failure_preserves_original_targets(self):
        app=self.app(); before=deepcopy(app.config.values)
        app.config.save.side_effect=OSError('只读')
        app.highlight_vault_var.set(str(self.c)); app._on_highlight_vault_changed()
        self.assertEqual(app.config.values,before)
        app._start_highlight_entry_sync.assert_not_called()

    def test_sync_uses_highlight_vault_and_cross_destination(self):
        app=self.app(); self.assertTrue(app._save_highlight_settings())
        bridge=mock.Mock()
        app._ensure_highlight_single_export(bridge,str(self.b),{})
        bridge.configure_highlight_single_export.assert_called_once_with('索引/摘录.md','注释',vault_path=str(self.c))
        old=mock.Mock()
        app._ensure_highlight_single_export(old,str(self.a),{})
        app._ensure_highlight_active_colors(old,str(self.a))
        old.configure_highlight_single_export.assert_not_called()
        old.configure_highlight_active_colors.assert_not_called()

    def test_legacy_plugin_is_rejected_before_cross_vault_requests(self):
        bridge=w.ObsidianBridgeClient.__new__(w.ObsidianBridgeClient)
        bridge._request=mock.Mock(return_value={'ok':True,'version':'2.5.0'})
        with self.assertRaises(w.ObsidianBridgeError):
            bridge.configure_highlight_single_export('索引.md','注释',str(self.c))
        self.assertEqual([c.args[0] for c in bridge._request.call_args_list],['/health'])
        bridge._request.reset_mock()
        with self.assertRaises(w.ObsidianBridgeError):
            bridge.export_highlights('导出',vault_path=str(self.c))
        self.assertEqual([c.args[0] for c in bridge._request.call_args_list],['/health'])

    def test_capable_plugin_receives_destination_for_both_operations(self):
        bridge=w.ObsidianBridgeClient.__new__(w.ObsidianBridgeClient)
        bridge._request=mock.Mock(side_effect=[{'cross_vault_export':True},{'config':{'vaultPath':str(self.c)}}])
        bridge.configure_highlight_single_export('索引.md','注释',str(self.c))
        self.assertEqual(bridge._request.call_args.args[1]['vault_path'],str(self.c))
        bridge._request=mock.Mock(side_effect=[{'cross_vault_export':True},{'vault_path':str(self.c)}])
        bridge.export_highlights('导出',vault_path=str(self.c))
        self.assertEqual(bridge._request.call_args.args[1]['vault_path'],str(self.c))

    def test_plugin_update_detects_open_vault_and_keeps_manual_fallbacks(self):
        p=self.root/'obsidian.json'
        p.write_text(json.dumps({'vaults':{'a':{'path':str(self.a),'ts':999},'b':{'path':str(self.b),'open':True,'ts':1}}}),encoding='utf-8')
        result=w.obsidian_plugin_update_candidates(str(self.a),str(self.c),p)
        self.assertEqual(result,[str(self.b),str(self.a),str(self.c)])
        p.write_text('invalid',encoding='utf-8')
        self.assertEqual(w.obsidian_plugin_update_candidates(str(self.a),str(self.c),p,highlight_vault=str(self.b)),[str(self.a),str(self.b),str(self.c)])

    def test_plugin_update_does_not_modify_record_or_export_configuration(self):
        app=self.app(); before=deepcopy(app.config.values)
        bridge_dir=self.c/'.obsidian/plugins'/w.OBSIDIAN_PLUGIN_ID
        bridge_dir.mkdir(parents=True)
        settings=bridge_dir/'data.json'; settings.write_text('{"token":"preserve"}',encoding='utf-8')
        app._set_obsidian_status=mock.Mock()
        with mock.patch.object(w.messagebox,'showinfo'):
            self.assertTrue(app._install_obsidian_plugin_to(str(self.c)))
        self.assertEqual(app.config.values,before)
        self.assertEqual(settings.read_text(encoding='utf-8'),'{"token":"preserve"}')
        self.assertEqual(json.loads((bridge_dir/'manifest.json').read_text(encoding='utf-8'))['version'],'2.5.4')

    def test_native_entry_retired_before_new_vault_enabled(self):
        app=self.app(); app._native_highlight_sync_lock=threading.Lock()
        app._native_highlight_sync_revision=0;app.capture_revision=4
        app.capture_mode=w.MODE_HIGHLIGHT;app.monitoring=True
        calls=[]
        def client(vault):
            return types.SimpleNamespace(configure_highlight_entry=lambda enabled:calls.append((vault,enabled)))
        with mock.patch.object(w,'ObsidianBridgeClient',side_effect=client),mock.patch.object(w.threading,'Thread',side_effect=immediate_thread):
            w.WordPickerApp._start_highlight_entry_sync(app,previous_vault=str(self.a))
        self.assertEqual(calls,[(str(self.a),False),(str(self.b),True)])
        self.assertGreater(app.capture_revision,4)

    @unittest.skipUnless(os.name == "nt", "Windows GUI integration")
    def test_complete_ui_question_save_leaves_invalid_highlight_settings_alone(self):
        config=w.AppConfig(self.root/'config.json')
        config.values.update(obsidian_vault_path=str(self.a), highlight_vault_path=str(self.b),
                             question_output_dir=str(self.a/'索引'),question_output_filename='问题.md',
                             question_body_dir=str(self.a/'正文'),question_materials_dir=str(self.a/'资料'),
                             highlight_export_dir=str(self.c/'导出'),highlight_single_index_dir=str(self.c/'索引'),
                             highlight_single_index_filename='摘录.md',highlight_single_note_dir=str(self.c/'正文'))
        root=tk.Tk(); root.withdraw()
        try:
            with mock.patch.object(w,'AppConfig',return_value=config),mock.patch.object(w.tk,'Tk',return_value=root), \
                 mock.patch.object(w,'VocabularyIndex'),mock.patch.object(w,'SourceLibrary'), \
                 mock.patch.object(w,'WindowsSelectionCapture'),mock.patch.object(w,'ObsidianBridgeClient'), \
                 mock.patch.object(w.messagebox,'showerror') as errors:
                app=w.WordPickerApp()
                root.update_idletasks()
                self.assertIsNot(app.highlight_vault_var,app.obsidian_vault_var)
                high_before={k:deepcopy(v) for k,v in config.values.items() if k.startswith('highlight_')}
                app.highlight_single_index_dir_var.set(str(self.root/'invalid-target'))
                app.highlight_single_note_var.set('')
                self.assertTrue(app._save_settings(),'question save must not validate unsaved highlighter controls')
                errors.assert_not_called()
                self.assertEqual({k:v for k,v in config.values.items() if k.startswith('highlight_')},high_before)
                # The updater opens a separate selector and does not set either vault variable.
                with mock.patch.object(w,'obsidian_plugin_update_candidates',return_value=[str(self.c)]):
                    app._install_obsidian_plugin()
                self.assertEqual(app.obsidian_vault_var.get(),str(self.a))
                self.assertEqual(app.highlight_vault_var.get(),str(self.b))
                popup=next(child for child in root.winfo_children() if isinstance(child,tk.Toplevel))
                popup.destroy()
        finally:
            root.destroy()

    def test_whole_export_reads_highlight_source_and_tags_destination(self):
        app=self.app(); bridge=mock.Mock()
        bridge.export_highlights.return_value={'count':2,'vault_path':str(self.c)}
        with mock.patch.object(w,'ObsidianBridgeClient',return_value=bridge) as client, mock.patch.object(w.threading,'Thread',side_effect=immediate_thread):
            app._export_highlights()
        client.assert_called_once_with(str(self.b))
        bridge.export_highlights.assert_called_once_with('全部高光','',vault_path=str(self.c))
        event=app.events.get_nowait()
        self.assertEqual(event[5],str(self.b))
        self.assertEqual(app.config.values['obsidian_vault_path'],str(self.a))

    def test_native_retirement_failure_is_retained_and_retried(self):
        app=self.app(); app._native_highlight_sync_lock=threading.Lock()
        app._native_highlight_sync_revision=0;app.capture_revision=0
        app.capture_mode=w.MODE_HIGHLIGHT;app.monitoring=True
        calls=[]; failing=True
        def client(vault):
            def configure(enabled):
                calls.append((vault,enabled))
                if vault==str(self.a) and failing:
                    raise w.ObsidianBridgeError('offline')
            return types.SimpleNamespace(configure_highlight_entry=configure)
        with mock.patch.object(w,'ObsidianBridgeClient',side_effect=client),mock.patch.object(w.threading,'Thread',side_effect=immediate_thread):
            w.WordPickerApp._start_highlight_entry_sync(app,previous_vault=str(self.a))
            self.assertIn(str(self.a),app._retired_highlight_vaults)
            self.assertTrue(app.events.get_nowait()[3])
            failing=False
            w.WordPickerApp._start_highlight_entry_sync(app)
        self.assertNotIn(str(self.a),app._retired_highlight_vaults)
        self.assertEqual(calls,[(str(self.a),False),(str(self.b),True)]*2)

    def test_old_single_export_acknowledgement_cannot_report_new_target_as_synced(self):
        app=self.app(); self.assertTrue(app._save_highlight_settings())
        app.root=mock.Mock();app.root.winfo_exists.return_value=True
        app._maybe_retry_highlight_single_export=mock.Mock()
        app._set_highlight_status.reset_mock();app._set_transient_status.reset_mock()
        app.events.put(('highlight_single_export_sync',True,'',str(self.b)))
        app._process_events()
        app._set_transient_status.assert_not_called()
        payload=app._highlight_single_export_payload()
        app._highlight_single_export_synced=app._highlight_single_export_identity(str(self.b),payload)
        app.events.put(('highlight_single_export_sync',True,'',str(self.b)))
        app._process_events()
        app._set_transient_status.assert_called_once_with('单条导出目标已同步到 Obsidian。')

    def test_old_native_mode_event_does_not_switch_current_mode(self):
        app=self.app();app.root=mock.Mock();app.root.winfo_exists.return_value=True
        app._switch_capture_mode=mock.Mock();app._maybe_retry_highlight_single_export=mock.Mock()
        app.events.put(('native_mode_switch',w.MODE_QUESTION,str(self.a)))
        app._process_events()
        app._switch_capture_mode.assert_not_called()


if __name__=='__main__':
    unittest.main()
