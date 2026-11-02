"""Combine NFI's maintained blacklist with ours into one config file (lists don't merge in freqtrade)."""
import json, re, sys
nfi_path, extra_path, out_path = sys.argv[1:4]
txt = re.sub(r"^\s*//.*$", "", open(nfi_path).read(), flags=re.M)
txt = re.sub(r",(\s*[\]}])", r"\1", txt)
nfi = json.loads(txt)["exchange"]["pair_blacklist"]
extra = json.load(open(extra_path))["pair_blacklist"]
json.dump({"exchange": {"pair_blacklist": nfi + extra}}, open(out_path, "w"), indent=1)
print(f"blacklist: {len(nfi)} NFI + {len(extra)} ours")
