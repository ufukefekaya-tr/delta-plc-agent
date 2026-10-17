from pathlib import Path
import tempfile
import unittest

from universal_plc.adapters import AdapterManager
from universal_plc.models import PLCError, TargetProfile
from universal_plc.registry import Registry
from universal_plc.service import Service
from universal_plc.storage import digest, inside, read_json, write_json


AX = {"vendor": "delta", "family": "AX", "model": "AX-308EA0MA1P"}
AS = {"vendor": "delta", "family": "AS", "model": "AS228T-A", "toolchain": "ispsoft"}


class CountingAdapter:
    def __init__(self):
        self.calls = []
    def capabilities(self):
        return [{"operation": "isp.inspect", "implementation": "available", "qualification": "software"}]
    def invoke(self, operation, arguments):
        self.calls.append((operation, arguments))
        return {"ok": True, "evidence_kind": "FILE_TRANSFORM", "vendor_compile": False}


class RoutingTests(unittest.TestCase):
    def setUp(self):
        self.registry = Registry()
    def resolve(self, value, **kwargs):
        return self.registry.resolve(TargetProfile.from_dict(value), **kwargs)
    def test_exact_ax_catalog_and_second_model(self):
        for model in ("AX-308EA0MA1P", "AX-532EB0MB1T", "AX-C24EB0MD1P"):
            self.assertEqual(self.resolve({**AX, "model": model})["adapter_id"], "delta.ax")
    def test_unknown_ax_is_not_nearest_model(self):
        with self.assertRaisesRegex(PLCError, "MODEL_NOT_CATALOGUED"):
            self.resolve({**AX, "model": "AX-999UNKNOWN"})
    def test_as100_cannot_route_to_isp(self):
        with self.assertRaisesRegex(PLCError, "INCOMPATIBLE"):
            self.resolve({**AS, "model": "AS132T-A"})
        self.assertEqual(self.resolve({**AS, "model": "AS132T-A", "toolchain": "diadesigner"})["adapter_id"], "delta.diadesigner")
    def test_fictional_models_in_real_families_rejected(self):
        for family,model in [('AS','AS299TOTALLYMADEUP'),('DVP','DVP999999SS2INVALID')]:
            with self.assertRaisesRegex(PLCError,'MODEL_NOT_CATALOGUED'):
                self.resolve(dict(vendor='delta',family=family,model=model,toolchain='ispsoft'))
    def test_as200_requires_tool_choice_without_format(self):
        value = dict(AS); value.pop("toolchain")
        self.assertTrue(self.resolve(value)["requires_selection"])
        self.assertEqual(self.resolve({**value, "project_format": ".isp"})["adapter_id"], "delta.ispsoft")
    def test_new_dvp_cannot_route_wpl(self):
        with self.assertRaisesRegex(PLCError, "INCOMPATIBLE"):
            self.resolve({"vendor":"delta", "family":"DVP", "model":"DVP22EX300T", "toolchain":"wplsoft"})
    def test_classic_dvp_and_tec_are_distinct(self):
        base={"vendor":"delta", "family":"DVP"}
        self.assertEqual(self.resolve({**base,"model":"DVP14SS211R","project_format":".dvp"})["adapter_id"],"delta.wplsoft")
        self.assertEqual(self.resolve({**base,"model":"DVP32ES300TEC"})["adapter_id"],"delta.diadesigner")
    def test_project_model_conflict(self):
        with self.assertRaisesRegex(PLCError,"conflict"):
            self.resolve(AS, project_target={"model":"AS324MT-A"})
    def test_missing_vendor_and_non_text_are_rejected(self):
        for value in ({"family":"AX","model":"AX-308EA0MA1P"},{**AX,"firmware":23}):
            with self.assertRaises(PLCError): TargetProfile.from_dict(value)
    def test_other_vendor_uses_same_core(self):
        document={"adapters":{"example.logic":{"toolchain":"example","formats":[".logic"],"file_operations":["isp.inspect"]}},"rules":[{"id":"test","vendor":"example","family":"LOGIC","model_pattern":"L100","adapters":["example.logic"],"evidence":"SYNTHETIC TEST ONLY"}]}
        value=Registry(document).resolve(TargetProfile.from_dict({"vendor":"example","family":"LOGIC","model":"L100"}))
        self.assertEqual(value["adapter_id"],"example.logic")
        self.assertFalse(value["native_verified"])


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory(); self.addCleanup(self.temporary.cleanup)
        self.root=Path(self.temporary.name); self.inputs=self.root/"inputs"; self.inputs.mkdir()
        self.adapter=CountingAdapter(); manager=AdapterManager(); manager.register("delta.ispsoft",self.adapter)
        self.service=Service(self.root/"state",read_roots=[self.inputs],adapters=manager)
        self.workspace=self.service.workspace_create(AS)
    def call(self,op="isp.inspect",args=None,digest_value=None):
        return self.service.invoke(self.workspace["workspace_id"],op,args or {},digest_value or self.workspace["context_digest"])
    def test_workspace_is_not_native_project(self):
        self.assertFalse(self.workspace["native_project_created"])
    def test_hash_locked_import_no_overwrite(self):
        source=self.inputs/"a.txt"; source.write_text("Türkçe")
        self.service.import_file(self.workspace["workspace_id"],str(source),"a.txt",digest(source))
        with self.assertRaises(PLCError):
            self.service.import_file(self.workspace["workspace_id"],str(source),"a.txt",digest(source))
    def test_import_outside_read_roots_rejected(self):
        source=self.root/"private.txt"; source.write_text("outside")
        with self.assertRaises(PLCError):
            self.service.import_file(self.workspace["workspace_id"],str(source),"a.txt",digest(source))
    def test_path_traversal_and_absolute_paths_rejected(self):
        for value in ("../outside",str(self.root/"outside"),"C:/outside", "a/../../outside"):
            with self.assertRaises(PLCError): self.call(args={"input_path":value})
        self.assertEqual(self.adapter.calls,[])
    def test_context_drift_rejected(self):
        with self.assertRaisesRegex(PLCError,"STALE_EXECUTION_CONTEXT"):
            self.call(digest_value="0"*64)
    def test_manifest_tampering_rejected(self):
        directory,_=self.service.workspace(self.workspace["workspace_id"])
        path=directory/"manifest.json"; data=read_json(path); data['target']['model']='AS324MT-A'; write_json(path,data,replace=True)
        with self.assertRaisesRegex(PLCError,"CONTEXT_DRIFT"): self.call()
    def test_original_baseline_must_not_change(self):
        source=self.inputs/"p.isp"; source.write_bytes(b"baseline")
        ws=self.service.workspace_create({**AS,"project_format":".isp"},str(source),digest(source))
        directory,_=self.service.workspace(ws['workspace_id']); (directory/'files'/ws['baseline_file']).write_bytes(b'changed')
        with self.assertRaisesRegex(PLCError,'BASELINE_DRIFT'): self.service.workspace(ws['workspace_id'])
    def test_unimplemented_compile_cannot_succeed(self):
        with self.assertRaisesRegex(PLCError,"NOT_IMPLEMENTED"): self.call("project.build")
        self.assertFalse(self.adapter.calls)
    def test_file_evidence_is_not_compile_evidence(self):
        result=self.call(); record=self.service.evidence_read(self.workspace['workspace_id'],result['evidence_id'])
        self.assertFalse(record['result']['vendor_compile'])
        self.assertEqual(record['status'],'returned')
    def test_workflow_once_even_after_host_restart(self):
        run=self.service.workflow_start(self.workspace['workspace_id'],'W1'); steps=[{'operation':'isp.inspect','arguments':{}}]
        first=self.service.workflow_run(self.workspace['workspace_id'],run['run_id'],steps)
        restarted=Service(self.root/'state',adapters=self.service.adapters)
        second=restarted.workflow_run(self.workspace['workspace_id'],run['run_id'],steps)
        self.assertEqual(first,second); self.assertEqual(len(self.adapter.calls),1)
    def test_unfinished_admission_never_replayed(self):
        run=self.service.workflow_start(self.workspace['workspace_id'],'W1')
        directory,_=self.service.workspace(self.workspace['workspace_id']); write_json(directory/'runs'/(run['run_id']+'.admitted.json'),{'crash':True})
        value=self.service.workflow_run(self.workspace['workspace_id'],run['run_id'],[{'operation':'isp.inspect','arguments':{}}])
        self.assertEqual(value['status'],'unknown_outcome'); self.assertFalse(self.adapter.calls)
    def test_workflow_rejects_live_operation_in_file_workflow(self):
        run=self.service.workflow_start(self.workspace['workspace_id'],'W4')
        with self.assertRaises(PLCError):
            self.service.workflow_run(self.workspace['workspace_id'],run['run_id'],[{'operation':'deployment.execute','arguments':{}}])
    def test_non_success_never_runs_dependent_steps(self):
        for response in ({'status':'cancelled'}, {'status':'cancel_requested'}, {'status':'fault'}, {'ok':False}, {'status':'unknown_future_state'}, {}):
            calls=[]
            def invoke(operation, arguments):
                calls.append(operation); return response
            self.adapter.invoke=invoke
            run=self.service.workflow_start(self.workspace['workspace_id'],'W1')
            step={'operation':'isp.inspect','arguments':{}}
            value=self.service.workflow_run(self.workspace['workspace_id'],run['run_id'],[step,step])
            self.assertNotEqual(value['status'],'steps_returned')
            self.assertEqual(len(calls),1)
    def test_target_forwarded_to_file_adapter(self):
        self.call()
        self.assertEqual(self.adapter.calls[0][1]['universal_target']['model'],AS['model'])
    def test_second_vendor_adapter_executes_without_delta_binding(self):
        catalog={'adapters':{'example.logic':{'toolchain':'example','formats':['.logic'],'file_operations':['isp.inspect']}},'rules':[{'id':'synthetic','vendor':'example','family':'LOGIC','model_pattern':'L100','adapters':['example.logic'],'evidence':'synthetic only'}]}
        manager=AdapterManager(); manager.register('example.logic',self.adapter)
        service=Service(self.root/'other',registry=Registry(catalog),adapters=manager)
        ws=service.workspace_create({'vendor':'example','family':'LOGIC','model':'L100'})
        value=service.invoke(ws['workspace_id'],'isp.inspect',{},ws['context_digest'])
        self.assertTrue(value['result']['ok'])
        self.assertEqual(self.adapter.calls[-1][1]['universal_target']['vendor'],'example')


if __name__ == "__main__":
    unittest.main()
