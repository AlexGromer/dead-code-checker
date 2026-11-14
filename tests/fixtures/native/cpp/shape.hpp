#pragma once
class Shape {
public:
    virtual ~Shape() {}
    virtual double area() const = 0;
    double twice() const;
};
class Square : public Shape {
public:
    double area() const override { return 4.0; }
};
class Orphan : public Shape {
public:
    double area() const override { return 0.0; }
};
class Counter {
public:
    Counter();
    ~Counter();
};
