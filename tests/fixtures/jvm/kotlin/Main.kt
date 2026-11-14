package com.acme.kt

import org.springframework.stereotype.Service

interface Greeter {
    fun greet(name: String): String
}

@Service
class PoliteGreeter(private val clock: Clock) : Greeter {
    override fun greet(name: String): String = "Dear " + name

    private fun unused() {}
}

class Clock

fun main(args: Array<String>) {
    println(args.size)
}
