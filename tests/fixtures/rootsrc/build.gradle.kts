plugins { application }
application {
    mainClass.set("com.example.KtApp")
}
tasks.jar { manifest { attributes["Main-Class"] = "com.example.KtCli" } }
