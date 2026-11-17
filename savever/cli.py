import argparse
import savever as m


def main():
    ap = argparse.ArgumentParser(description="存档版本迁移")
        ap.add_argument("--to", type=int, required=True)
        ap.add_argument("--out", required=True)
    a = ap.parse_args()
        print("TODO")


if __name__ == "__main__":
    main()
