import math


def circle_area(r: float) -> float:
    return math.pi * r * r


def rectangle_area(w: float, h: float) -> float:
    """Area of a rectangle."""
    return w * h


class Triangle:
    def __init__(self, a: float, b: float, c: float):
        self.a, self.b, self.c = a, b, c

    def perimeter(self) -> float:
        return self.a + self.b + self.c

    def area(self) -> float:
        """Heron's formula."""
        s = self.perimeter() / 2
        return math.sqrt(s * (s - self.a) * (s - self.b) * (s - self.c))
