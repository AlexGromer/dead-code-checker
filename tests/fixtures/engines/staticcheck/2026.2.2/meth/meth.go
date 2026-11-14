package meth

type t struct{}

func (*t) dead() {}

func (t) dead2() {}

func (*t) used() {}

func Use() { (&t{}).used() }
