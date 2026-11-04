import unittest
from types import SimpleNamespace

from src.annotations import map_labels, polygon_to_yolo, rectangle_to_yolo


class AnnotationConversionTests(unittest.TestCase):
    def test_rectangle_normalizes_and_clamps(self):
        result = rectangle_to_yolo([-10, 10, 50, 90], 100, 100)
        self.assertEqual(result, (0.25, 0.5, 0.5, 0.8))

    def test_polygon_preserves_vertex_order(self):
        result = polygon_to_yolo([0, 0, 100, 0, 50, 100], 100, 100)
        self.assertEqual(result, [0, 0, 1, 0, 0.5, 1])

    def test_label_mapping_uses_name_not_cross_project_id(self):
        source = {1: "Pedra", 2: "Minério"}
        destination = [SimpleNamespace(id=90, name="Pedra"), SimpleNamespace(id=4, name="Minério")]
        self.assertEqual(map_labels(source, destination), {1: 90, 2: 4})

    def test_missing_label_is_clear_error(self):
        with self.assertRaisesRegex(ValueError, "ausentes"):
            map_labels({7: "Pedra"}, [SimpleNamespace(id=8, name="Areia")])


if __name__ == "__main__":
    unittest.main()
