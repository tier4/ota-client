import sys
import semantic_version

v = semantic_version.Version(sys.argv[1])
print(f"{v.major}.{v.minor + 1}.{v.patch}")
