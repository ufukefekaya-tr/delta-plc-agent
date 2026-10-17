from __future__ import annotations
import copy
import importlib.util
from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
import unittest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("native_project_factory", ROOT / "backends/ax/adapters/ax_worker/project_factory.py")
factory = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(factory)


class Text:
    text = ""
    def replace(self, value): self.text = value


class Calls(list):
    def add(self, name, comment): self.append((name, comment))


class Node:
    def __init__(self, name, **values):
        self.name, self.children = name, []
        self.__dict__.update(values)
    def get_name(self): return self.name
    def get_children(self, recursive):
        return self.children + ([child for item in self.children for child in item.get_children(True)] if recursive else [])
    def create_pou(self, name, _kind, _language):
        node = Node(name, textual_declaration=Text(), textual_implementation=Text())
        self.children.append(node)
        return node
    def create_task(self, name):
        node = Node(name, is_task=True, pous=Calls())
        self.children.append(node)
        return node


class Projects:
    primary = None
    def __init__(self): self.saved = None; self.drift = False
    def create(self, output, primary):
        owner = self
        class Project(Node):
            def add(self, name, device_type, device_id, version):
                device = Node(name, get_device_identification=lambda: NS(type=device_type, id=device_id, version=version))
                app = Node("Application", is_application=True)
                tc = Node("Task Configuration", is_task_configuration=True)
                native = tc.create_task("NativeTask")
                native.pous.add("VendorProgram", "Vendor binding")
                app.children = [tc]; device.children = [app]; self.children = [device]
            def save(self):
                Path(output).write_bytes(b"synthetic project; not vendor binary")
                owner.saved = self
            def close(self): owner.primary = None
        self.primary = Project("Project")
        return self.primary
    def open(self, output, primary):
        self.primary = self.saved
        # Native task calls are tree objects sharing the textual POU name.
        app = self.primary.children[0].children[0]
        for task in app.children[0].children:
            task.children = [Node(name) for name, _comment in task.pous]
        if self.drift:
            app.children[0].children[0].pous[0] = ("ChangedProgram", "drift")
        return self.primary


class ProjectFactoryTests(unittest.TestCase):
    def config(self, root):
        return {"output_path": str(root / "neutral.project"), "project_id": "NeutralProject",
                "device_profile": {"profile_id": "synthetic", "model": "AX-TEST", "device_type": 4102, "device_id": "0000 0001", "device_version": "1.0.0"},
                "controller_name": "Controller", "pou_name": "MainProgram", "task_name": "UniversalTask", "interval_ms": 10,
                "declaration": "PROGRAM MainProgram\nVAR\nEND_VAR", "implementation": "// no outputs"}
    def run_factory(self, projects, config):
        return factory.create_project(projects, NS(Program="program"), NS(st="st"), NS(Cyclic="cyclic"), config)
    def test_preserves_vendor_task_and_schedules_new_pou_with_readback(self):
        with tempfile.TemporaryDirectory() as tmp:
            projects = Projects()
            result = self.run_factory(projects, self.config(Path(tmp)))
            self.assertEqual(result["status"], "created")
            self.assertFalse(result["compile_verified"])
            self.assertFalse(result["live_verified"])
            tasks = projects.saved.children[0].children[0].children[0].children
            self.assertEqual([t.name for t in tasks], ["NativeTask", "UniversalTask"])
            self.assertEqual(tasks[0].pous, [("VendorProgram", "Vendor binding")])
            self.assertEqual(tasks[1].pous[0][0], "MainProgram")
    def test_task_collision_does_not_overwrite_vendor_binding(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = self.config(Path(tmp)); config["task_name"] = "NativeTask"
            projects = Projects()
            with self.assertRaisesRegex(factory.ProjectCreateError, "unique task"):
                self.run_factory(projects, config)
            self.assertIsNone(projects.saved)
    def test_existing_file_and_open_primary_are_never_replaced(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = self.config(Path(tmp))
            Path(config["output_path"]).write_bytes(b"original")
            with self.assertRaises(factory.ProjectCreateError): self.run_factory(Projects(), config)
            self.assertEqual(Path(config["output_path"]).read_bytes(), b"original")
            config["output_path"] = str(Path(tmp) / "other.project")
            projects = Projects(); projects.primary = object()
            with self.assertRaises(factory.ProjectCreateError): self.run_factory(projects, config)
    def test_saved_vendor_task_drift_is_not_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            projects = Projects(); projects.drift = True
            with self.assertRaisesRegex(factory.ProjectCreateError, "bindings changed"):
                self.run_factory(projects, self.config(Path(tmp)))


if __name__ == "__main__": unittest.main()
