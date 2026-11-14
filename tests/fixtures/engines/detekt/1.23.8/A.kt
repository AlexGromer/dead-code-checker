package foo.bar

import java.util.List
import java.io.File

class my_class(val Bad_name: Int) {
    private val unusedProp = 1
    private fun unusedFun(x: Int) {}
    fun Good_Fun(p: Int) { val Local_Var = 2; println(Local_Var) }
}

private class Hidden
