import torch
from torch.fx.symbolic_trace import symbolic_trace
from torch.fx.experimental import GraphManipulation
from torch.fx.experimental.Partitioner import Partitioner, Device, PartitionerConfig
from torch.fx.experimental.rewriter import RewritingTracer
from torch.fx.graph_module import GraphModule
from torch.testing._internal.common_utils import run_tests
from torch.testing._internal.jit_utils import JitTestCase
from typing import Union, Callable

def symbolic_trace_with_rewrite(root: Union[torch.nn.Module, Callable]) -> GraphModule:
    return GraphModule(root if isinstance(root, torch.nn.Module) else torch.nn.Module(), RewritingTracer().trace(root))

class TestFXExperimental(JitTestCase):
    def test_find_single_partition(self):
        class TestModule(torch.nn.Module):
            def forward(self, a, b):
                return a + b
        m = TestModule()
        traced = symbolic_trace(m)
        a = torch.rand(1)
        b = torch.rand(1)
        GraphManipulation.get_size_of_all_nodes(
            traced,
            [a, b]
        )
        partitioner = Partitioner()
        devices = [
            Device('dev_0', 125, 0),
            Device('dev_1', 125, 1),
            Device('dev_2', 125, 2)
        ]
        partitioner_config = PartitionerConfig(devices)
        ret = partitioner.partition_graph(traced, m, partitioner_config)
        module_with_submodules = ret.module_with_submodules
        dag = ret.dag
        self.assertEqual(traced(a, b), module_with_submodules(a, b))
        assert dag.nodes[0].logical_device_ids == [0]

    def test_size_based_partition(self):
        class TestModule(torch.nn.Module):
            def __init__(self):
                super().__init__()
                self.linear = torch.nn.Linear(4, 4)

            def forward(self, a, b):
                add_1 = a + b
                linear = self.linear(add_1)
                e = torch.rand(4)
                add_2 = linear + e
                return add_2

        m = TestModule()
        traced = symbolic_trace(m)
        a = torch.rand(4)
        b = torch.rand(4)
        GraphManipulation.get_size_of_all_nodes(
            traced,
            [a, b]
        )
        partitioner = Partitioner()
        devices = [
            Device('dev_0', 125, 0),
            Device('dev_1', 125, 1),
            Device('dev_2', 125, 2)
        ]
        partitioner_config = PartitionerConfig(devices)
        ret = partitioner.partition_graph(traced, m, partitioner_config)
        module_with_submodules = ret.module_with_submodules
        dag = ret.dag
        self.assertEqual(traced(a, b), module_with_submodules(a, b))
        for i, node in enumerate(dag.nodes):
            assert node.logical_device_ids == [i]

    def test_partition_combining(self):
        class TestModule(torch.nn.Module):
            def __init__(self):
                super().__init__()
                self.linear = torch.nn.Linear(4, 4)

            def forward(self, a):
                b = torch.rand(4)
                add_1 = a + b
                linear_1 = self.linear(add_1)
                add_2 = torch.rand(4) + a
                add_3 = add_2 + linear_1
                return add_3

        m = TestModule()
        traced = symbolic_trace(m)
        a = torch.rand(4)
        GraphManipulation.get_size_of_all_nodes(
            traced,
            [a]
        )
        partitioner = Partitioner()
        devices = [
            Device('dev_0', 120, 0),
            Device('dev_1', 144, 1)
        ]
        partitioner_config = PartitionerConfig(devices, is_sparse_nn=False)
        ret = partitioner.partition_graph(traced, m, partitioner_config)
        module_with_submodules = ret.module_with_submodules
        dag = ret.dag
        self.assertEqual(traced(a), module_with_submodules(a))
        assert dag.nodes[0].logical_device_ids == [0]
        assert dag.nodes[0].size_bytes == 80
        assert dag.nodes[1].logical_device_ids == [1]
        assert dag.nodes[1].size_bytes == 144

    def test_sparse_nn_partition(self):
        class MyRecommendationModule(torch.nn.Module):
            def create_mlp(self, num_of_layers: int, input_size: int, output_size: int):
                layers = torch.nn.ModuleList()
                for _ in range(num_of_layers):
                    ll = torch.nn.Linear(input_size, output_size)
                    layers.append(ll)
                    layers.append(torch.nn.ReLU())
                return layers

            def __init__(self):
                super(MyRecommendationModule, self).__init__()
                layers = self.create_mlp(4, 4, 4)
                self.bottom_layers = torch.nn.Sequential(*layers)
                layers = self.create_mlp(3, 24, 24)
                self.top_layers = torch.nn.Sequential(*layers)
                self.embedding_layers = torch.nn.ModuleList()
                el = torch.nn.EmbeddingBag(500000, 4, mode='sum', sparse=True)
                self.embedding_layers.append(el)
                for i in range(3):
                    el = torch.nn.EmbeddingBag(1000000, 4, mode='sum', sparse=True)
                    self.embedding_layers.append(el)
                el = torch.nn.EmbeddingBag(500000, 4, mode='sum', sparse=True)
                self.embedding_layers.append(el)

            def forward(self, a, b, offset):
                x = self.bottom_layers(a)
                y = []
                c = []
                for i in range(len(self.embedding_layers)):
                    temp = torch.randint(10, (8, ))
                    c.append(temp + b)
                for i in range(len(self.embedding_layers)):
                    if i % 2 == 0:
                        y.append(self.embedding_layers[i](c[i], offset))
                    else:
                        y.append(self.embedding_layers[i](torch.randint(10, (8, )), offset))
                z = torch.cat([x] + y, dim=1)
                p = self.top_layers(z)
                return p

        m = MyRecommendationModule()
        a = torch.rand(2, 4)
        b = torch.randint(10, (8, ))
        offset = torch.randint(1, (2, ))
        traced = symbolic_trace(m)
        GraphManipulation.get_size_of_all_nodes(traced, [a, b, offset])
        devices = [
            Device('dev_0', 33000000, 0),
            Device('dev_1', 33000000, 1),
            Device('dev_2', 33000000, 2)
        ]
        partitioner_config = PartitionerConfig(devices, is_sparse_nn=True)
        partitioner = Partitioner()
        ret = partitioner.partition_graph(traced, m, partitioner_config)
        module_with_submodules = ret.module_with_submodules
        dag = ret.dag
        self.assertEqual(traced(a, b, offset), module_with_submodules(a, b, offset))
        assert len(module_with_submodules.graph.nodes) == 24

    def test_call_to_assert_no_msg(self):

        class M(torch.nn.Module):
            def forward(self, a, b):
                assert a == b
                return a + b
        m = M()
        traced = symbolic_trace_with_rewrite(m)

        # Make sure the graph is well-formed
        traced.graph.lint(traced)

        # Check the IR to make sure there's a call_function node with target == "Assert"
        self.assertTrue(any(node.op == "call_function" and node.target == torch.Assert for node in traced.graph.nodes))

        # Ensure that the assert throws when it's supposed to and doesn't throw when it's not supposed to
        traced(3, 3)
        with self.assertRaisesRegex(AssertionError, ""):
            traced(3, 5)

        # Confirm that the output is correct
        self.assertEqual(traced(3, 3), m(3, 3))

    def test_call_to_assert_with_msg(self):

        class M(torch.nn.Module):
            def forward(self, a, b):
                assert a == b, "test message"
                return a + b
        m = M()
        traced = symbolic_trace_with_rewrite(m)

        # Make sure the graph is well-formed
        traced.graph.lint(traced)

        # Check the IR to make sure there's a call_function node with target == "Assert"
        self.assertTrue(any(node.op == "call_function" and node.target == torch.Assert for node in traced.graph.nodes))

        # Ensure that the assert throws when it's supposed to and doesn't throw when it's not supposed to
        traced(3, 3)
        with self.assertRaisesRegex(AssertionError, "test message"):
            traced(3, 5)

        # Confirm that the output is correct
        self.assertEqual(traced(3, 3), m(3, 3))

    def test_call_to_assert_with_empty_msg(self):

        class M(torch.nn.Module):
            def forward(self, a, b):
                assert a == b, ""
                return a + b
        m = M()
        traced = symbolic_trace_with_rewrite(m)

        # Make sure the graph is well-formed
        traced.graph.lint(traced)

        # Check the IR to make sure there's a call_function node with target == "Assert"
        self.assertTrue(any(node.op == "call_function" and node.target == torch.Assert for node in traced.graph.nodes))

        # Ensure that the assert throws when it's supposed to and doesn't throw when it's not supposed to
        traced(3, 3)
        with self.assertRaisesRegex(AssertionError, ""):
            traced(3, 5)

        # Confirm that the output is correct
        self.assertEqual(traced(3, 3), m(3, 3))

    def test_call_to_assert_with_multiline_message(self):

        class M(torch.nn.Module):
            def forward(self, a, b):
                error_msg = """
An error message with
terrible spacing
                """
                assert a == b, error_msg
                return a + b
        m = M()
        traced = symbolic_trace_with_rewrite(m)

        # Make sure the graph is well-formed
        traced.graph.lint(traced)

        # Check the IR to make sure there's a call_function node with target == "Assert"
        self.assertTrue(any(node.op == "call_function" and node.target == torch.Assert for node in traced.graph.nodes))

        # Ensure that the assert throws when it's supposed to and doesn't throw when it's not supposed to
        error_msg = """
An error message with
terrible spacing
    """
        traced(3, 3)
        with self.assertRaisesRegex(AssertionError, error_msg):
            traced(3, 5)

        # Confirm that the output is correct
        self.assertEqual(traced(3, 3), m(3, 3))

    def test_traceable_function_with_nonstandard_name(self):
        def foo(x):
            return torch.relu(x)
        traced = symbolic_trace_with_rewrite(foo)

if __name__ == '__main__':
    run_tests()
