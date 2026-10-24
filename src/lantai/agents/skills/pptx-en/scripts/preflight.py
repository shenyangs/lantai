from lantai.office_skill_runtime import main

if __name__ == "__main__":
    raise SystemExit(main(["--kind", "pptx", *(__import__("sys").argv[1:])]))
