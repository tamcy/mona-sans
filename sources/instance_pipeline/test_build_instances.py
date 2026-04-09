import unittest

from build_instances import (
    InstanceRequest,
    apply_global_axis_overrides,
    parse_axis_assignment,
    parse_inline_instance,
)


class ParseTests(unittest.TestCase):
    def test_parse_axis_assignment(self):
        tag, value = parse_axis_assignment("wght=150")
        self.assertEqual(tag, "wght")
        self.assertEqual(value, 150.0)

    def test_parse_inline_instance(self):
        req = parse_inline_instance("Trial:wdth=100,wght=150,opsz=16,ital=0")
        self.assertEqual(req.name, "Trial")
        self.assertEqual(req.location["wdth"], 100.0)
        self.assertEqual(req.location["wght"], 150.0)

    def test_global_override(self):
        reqs = [InstanceRequest(name="A", location={"wght": 150, "wdth": 100})]
        updated = apply_global_axis_overrides(reqs, {"wght": 180})
        self.assertEqual(updated[0].location["wght"], 180)
        self.assertEqual(updated[0].location["wdth"], 100)


if __name__ == "__main__":
    unittest.main()

