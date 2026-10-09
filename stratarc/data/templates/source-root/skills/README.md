# skills

Skills are packaged instructions an agent loads when a task matches them. Each skill is a directory `skills/<kebab-name>/` holding a `SKILL.md` whose YAML frontmatter carries `name` (equal to the directory name) and `description` (what the skill does and when to use it, since runtimes match tasks against this text), followed by a Markdown body with the instructions; supporting files such as references or scripts sit beside `SKILL.md` in the same directory.
