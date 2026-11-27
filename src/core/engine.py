from typing import List, Dict, Any, Set
from collections import defaultdict, deque

class DAGEngine:
    """
    Manages Component DAG dependencies, performs topological sorting,
    and calculates sequential/parallel execution batches.
    """

    def __init__(self, components: List[Dict[str, Any]]):
        self.components_by_name = {c["name"]: c for c in components}
        self.graph = defaultdict(list)
        self.in_degree = defaultdict(int)
        self._build_graph()

    def _build_graph(self):
        for name in self.components_by_name:
            if name not in self.in_degree:
                self.in_degree[name] = 0

        for name, comp in self.components_by_name.items():
            deps = comp.get("dependencies", [])
            for dep in deps:
                if dep not in self.components_by_name:
                    raise ValueError(f"Component '{name}' has unsatisfied dependency '{dep}'")
                self.graph[dep].append(name)
                self.in_degree[name] += 1

    def get_execution_plan(self) -> List[List[Dict[str, Any]]]:
        """
        Returns execution batches where all components in the same batch
        can run in parallel without dependency conflicts (Kahn's Algorithm).
        """
        in_deg = dict(self.in_degree)
        queue = deque([name for name, deg in in_deg.items() if deg == 0])
        batches = []
        visited_count = 0

        while queue:
            current_batch = []
            next_level = []
            
            while queue:
                node = queue.popleft()
                current_batch.append(self.components_by_name[node])
                visited_count += 1

                for neighbor in self.graph[node]:
                    in_deg[neighbor] -= 1
                    if in_deg[neighbor] == 0:
                        next_level.append(neighbor)

            batches.append(current_batch)
            queue.extend(next_level)

        if visited_count != len(self.components_by_name):
            raise ValueError("Cyclic dependency detected in Component DAG definition!")

        return batches

    def get_linear_order(self) -> List[Dict[str, Any]]:
        batches = self.get_execution_plan()
        flat_list = []
        for b in batches:
            flat_list.extend(b)
        return flat_list

    def render_ascii_dag(self) -> str:
        batches = self.get_execution_plan()
        lines = ["=== COMPONENT DAG EXECUTION GRAPH ==="]
        for i, batch in enumerate(batches, 1):
            names = [f"[{c['name']}]" for c in batch]
            lines.append(f"  Step {i}: " + "  &  ".join(names))
            if i < len(batches):
                lines.append("      |")
                lines.append("      v")
        return "\n".join(lines)
