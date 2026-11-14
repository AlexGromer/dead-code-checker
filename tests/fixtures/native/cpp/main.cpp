#include "shape.hpp"
int main() {
    Square s;
    Counter c;
    Shape* p = &s;
    return (int)p->twice();
}
