params = {
    "topic": {"type": "string", "default": "a pirate ship made of GPUs", "doc": "what the haiku is about"},
}

write = agent(
    name = "write",
    prompt = prompt_file("write.md") + "\n\nTopic: " + param("topic") + "\n",
    emits = ["title"],
    emits_files = ["HAIKU.md"],
    timeout = "10m",
)

count = command(
    name = "count",
    run = "python3 count.py",
    depends_on = [write],
    emits = ["words"],
    emits_files = ["REPORT.md"],
)

workflow(type = "playbook", tasks = [write, count], result = count)
