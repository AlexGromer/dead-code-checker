package main

func main() { used() }

func used() {}

func deadFn() {}

func unusedHelper() { deadFn() }
