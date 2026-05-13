import zlib
import nbtlib
import io
import numpy as np
import sys
import math
import numba
import argparse
import pathlib
import copy
import re
import hashlib
import json
import base64

cache_fname = "mca_restorer_cache.json"

SECSIZE = 4096

class Region:
    def __init__(self, fname, world_offset=None):
        with open(fname, "rb") as f:
            data = f.read()
        assert len(data) >= SECSIZE * 2, f"The file should be at least 2 sectors in lengt; length found: {len(data)} bytes = {len(data) / SECSIZE} sectors"
        if len(data) % SECSIZE != 0:
            print(f"WARNING: fine size is not a multiple of sector size {SECSIZE}; got {SECSIZE}*{len(data) // SECSIZE}+{len(data) % SECSIZE}; padding with zeros")
            data += b"\0" * (SECSIZE - (len(data) % SECSIZE))
        self.data = data
        self.loc_table = { (i % 32, i // 32): int.from_bytes(data[i * 4: i * 4 + 4], "big") for i in range(1024) }
        self.validate_table()
        self.chunks = {}
        self.world_offset = world_offset
    
    def compute_data_hash(self):
        h = hashlib.sha256()
        h.update(self.data)
        return h.hexdigest()
    
    def validate_table(self):
        used_sectors = set()
        for loc in self.loc_table.values():
            for i in range(loc >> 8, (loc >> 8) + (loc & 0xFF)):
                assert i not in used_sectors, "Locations overlap"
                assert i < len(self.data) // SECSIZE, "Location out of range"
                used_sectors.add(i)
    
    def get_chunk_raw(self, x, z):
        loc = self.loc_table[x, z]
        pos = loc >> 8
        sec_size = loc & 0xFF
        length = int.from_bytes(self.data[pos * SECSIZE: pos * SECSIZE + 4], "big", signed=True)
        if sec_size == 0: return
        assert length > 0, "Non-positive length"
        assert length + 4 <= sec_size * SECSIZE, "Length out of range"
        return self.data[pos * SECSIZE + 4: pos * SECSIZE + 4 + length]
    
    def parse_chunk(self, x, z):
        raw_data = self.get_chunk_raw(x, z)
        if raw_data is None: return
        timestamp = int.from_bytes(self.data[SECSIZE + (x + z * 32) * 4: SECSIZE + (x + z * 32) * 4 + 4], "big")
        compression_type = raw_data[0]
        assert compression_type in [2, 3], "Only `Zlib` and `uncompressed` compression schemes are supported"
        if compression_type == 3: data = raw_data[1:]
        if compression_type == 2: data = zlib.decompress(raw_data[1:])
        f = io.BytesIO(data)
        nbt = nbtlib.File.from_fileobj(f)
        if nbt["Status"] != "minecraft:full": return
        if self.world_offset is None:
            self.world_offset = int(nbt["xPos"]) // 32, int(nbt["zPos"]) // 32
        blocks = chunk_nbt2blocks(nbt)
        self.chunks[x, z] = {
            "data": data, "nbt": nbt, "blocks": blocks, "timestamp": timestamp, "raw": raw_data
        }
        return self.chunks[x, z]
    
    def parse_chunks_normal(self):
        for x in range(32):
            for y in range(32):
                self.parse_chunk(x, y)
    
    def parse_chunks_notable(self):
        i = SECSIZE * 2
        n = 0
        while i < len(self.data):
            length = int.from_bytes(self.data[i: i + 4], "big")
            if length < 2 or i + length > len(self.data):
                i += SECSIZE; continue
            compression_type = self.data[i + 4]
            if compression_type not in [2, 3]:
                i += SECSIZE; continue
            if compression_type == 3: data = self.data[i + 5: i + 4 + length]
            elif compression_type == 2:
                try: data = zlib.decompress(self.data[i + 5: i + 4 + length])
                except: i += SECSIZE; continue
            f = io.BytesIO(memoryview(data))
            try: nbt = nbtlib.File.from_fileobj(f)
            except: i += SECSIZE; continue
            if "sections" not in nbt:
                i += SECSIZE; continue
            if self.world_offset is None:
                self.world_offset = int(nbt["xPos"]) // 32, int(nbt["zPos"]) // 32
            blocks = chunk_nbt2blocks(nbt)
            chunk = { "data": data, "nbt": nbt, "blocks": blocks }
            self.chunks[id(chunk)] = chunk
            n += 1
            i += SECSIZE * ((length + 4 - 1) // SECSIZE + 1)
        return n
    
    def extract_features(self):
        n = 0
        for i in self.chunks:
            n += 1
            blocks = self.chunks[i]["blocks"]
            features = np.empty_like(blocks, dtype=bool)
            get_features(features, blocks)
            self.chunks[i]["features"] = features
            self.chunks[i]["slice_features"] = features.reshape((16, -1, 8, 16)).astype(int).sum(axis=2)

    def apply_permutation(self, perm):
        chunks_new = {}
        for xz in perm:
            chunks_new[xz] = copy.deepcopy(self.chunks[perm[xz]])
        for x, z in chunks_new:
            chunk = chunks_new[x, z]
            chunk["nbt"]["xPos"] = type(chunk["nbt"]["xPos"])(x + self.world_offset[0] * 32)
            chunk["nbt"]["zPos"] = type(chunk["nbt"]["zPos"])(z + self.world_offset[1] * 32)
            f = io.BytesIO()
            chunk["nbt"].write(f)
            f.seek(0)
            chunk["data"] = f.read()
        self.chunks = chunks_new
    
    def chunks2data(self):
        f = io.BytesIO()
        head = io.BytesIO()
        # timestamps = io.BytesIO()
        sectors_written = 2
        for i in range(1024):
            x, z = i % 32, i // 32
            if (x, z) not in self.chunks:
                head.write((sectors_written << 8).to_bytes(4, "big"))
                # timestamps.write(b"\x00" * 4)
                continue
            chunk = self.chunks[x, z]
            raw_data = zlib.compress(chunk["data"])
            l = len(raw_data) + 5
            sectors = (l - 1) // SECSIZE + 1
            head.write(((sectors_written << 8) | sectors).to_bytes(4, "big"))
            f.write((l - 4).to_bytes(4, "big"))
            f.write(b"\x02")
            f.write(raw_data)
            f.write(b"\x02" * (sectors * SECSIZE - l))
            sectors_written += sectors
            # timestamps.write(chunk["timestamp"].to_bytes(4, "big"))
        # timestamps.seek(0)
        head.write(b"\x00" * SECSIZE)
        f.seek(0); head.write(f.read())
        head.seek(0); self.data = head.read()
    
    def save(self, fname):
        with open(fname, "wb") as f:
            f.write(self.data)

@numba.njit
def nums2arr(arr, nums, bit_length, blocks_per_num):
    mask = 2**bit_length - 1
    for i in range(16**3):
        arr[i] = (nums[i // blocks_per_num] >> ((i % blocks_per_num) * bit_length)) & mask

def blockname_hash(name):
    if name in ["bedrock", "minecraft:bedrock"]: return 1
    return hash(name)

def chunk_nbt2blocks(nbt):
    dtype = "int" + str(sys.hash_info.width)
    parsed_sections = {}
    for section in nbt["sections"]:
        y = int(section["Y"]) * 16
        if "block_states" not in section: continue
        if "palette" not in section["block_states"]: continue
        palette = np.array([blockname_hash(str(name["Name"])) for name in section["block_states"]["palette"]], dtype=dtype)
        if len(palette) == 1:
            sec = np.full((16, 16, 16), palette[0], dtype=dtype)
        else:
            if "data" not in section["block_states"]: continue
            bit_length = max(math.ceil(math.log2(len(palette))), 4)
            blocks_per_num = 64 // bit_length
            n_nums = (16**3 - 1) // blocks_per_num + 1
            nums = np.array(section["block_states"]["data"], dtype="uint64")
            assert len(nums) == n_nums
            indices = np.empty(16**3, dtype=dtype)
            nums2arr(indices, nums, bit_length, blocks_per_num)
            sec = palette[indices].reshape(16, 16, 16).transpose(2, 0, 1)
        parsed_sections[y] = sec
    # y_min = min(parsed_sections)
    # y_max = max(parsed_sections) + 16
    if nbt["yPos"] == -4:
        y_min = -64
        y_max = 320
    elif nbt["yPos"] == 0:
        y_min = 0
        y_max = 256
    else: raise ValueError(f"Unknown yPos: {nbt["yPos"]}")
    # elif nbt["yPos"] == -4
    chunk = np.zeros((16, y_max - y_min, 16), dtype=dtype)
    for y in parsed_sections:
        chunk[:, y - y_min: y - y_min + 16, :] = parsed_sections[y]
    return chunk

@numba.njit
def get_features(features, blocks):
    for x in range(blocks.shape[0]):
        for y in range(blocks.shape[1]):
            for z in range(blocks.shape[2]):
                feature = False
                b = blocks[x, y, z]
                # for [_x, _y, _z] in [[x - 1, y, z], [x + 1, y, z], [x, y - 1, z], [x, y + 1, z], [x, y, z - 1], [x, y, z + 1]]:
                #     if 0 <= _x < blocks.shape[0] and 0 <= _y < blocks.shape[1] and 0 <= _z < blocks.shape[2]:
                #         feature = feature or blocks[_x, _y, _z] == b
                if 0 <= x - 1 < blocks.shape[0]: feature = feature or blocks[x - 1, y, z] != b
                if 0 <= x + 1 < blocks.shape[0]: feature = feature or blocks[x + 1, y, z] != b
                if 0 <= y - 1 < blocks.shape[0]: feature = feature or blocks[x, y - 1, z] != b
                if 0 <= y + 1 < blocks.shape[0]: feature = feature or blocks[x, y + 1, z] != b
                if 0 <= z - 1 < blocks.shape[0]: feature = feature or blocks[x, y, z - 1] != b
                if 0 <= z + 1 < blocks.shape[0]: feature = feature or blocks[x, y, z + 1] != b
                features[x, y, z] = feature

def bedrock_matcher(chunk, template):
    return ((chunk["blocks"] == 1) != (template["blocks"] == 1)).sum()

def diff_matcher(chunk, template):
    return (chunk["blocks"] != template["blocks"]).sum()

@numba.njit
def xz_erode(eroded, spans):
    for x in range(spans.shape[0]):
        for y in range(spans.shape[1]):
            for z in range(spans.shape[2]):
                ok = spans[x, y, z]
                if 0 < x - 1 <= spans.shape[0]: ok &= spans[x - 1, y, z]
                if 0 < x + 1 <= spans.shape[0]: ok &= spans[x + 1, y, z]
                if 0 < z - 1 <= spans.shape[2]: ok &= spans[x, y, z - 1]
                if 0 < z + 1 <= spans.shape[2]: ok &= spans[x, y, z + 1]
                eroded[x, y, z] = ok

def features_matcher(chunk, template):
    chunk_slices = chunk["blocks"].reshape((16, -1, 8, 16))
    template_slices = template["blocks"].reshape((16, -1, 8, 16))
    _matching_span = (chunk_slices == template_slices).all(axis=2)
    matching_span = np.empty_like(_matching_span)
    xz_erode(matching_span, _matching_span)
    # print(matching_span.all(axis=0).all(axis=1))
    # print((template["slice_features"] * matching_span).sum(axis=0).sum(axis=1))
    # print((matching_span).sum(axis=0).sum(axis=1))
    r = (template["slice_features"] * matching_span).sum()
    return r

matchers = { "bedrock": bedrock_matcher, "diff": diff_matcher, "features": features_matcher }
resolvers = {
    "max-diff": lambda chunk, template: -diff_matcher(chunk, template),
    "min-diff": diff_matcher,
    "max-features": lambda chunk, template: -chunk["slice_features"].sum(),
    "min-features": lambda chunk, template:  chunk["slice_features"].sum(),
    "features-similar": features_matcher,
    "features-different": lambda chunk, template: -features_matcher(chunk, template)
}

def array2str(arr):
    buf = io.BytesIO()
    np.save(buf, arr)
    data = buf.getvalue()
    return base64.b64encode(zlib.compress(data)).decode("utf-8")

def str2array(s):
    data = base64.b64decode(s)
    buf = io.BytesIO(zlib.decompress(data))
    return np.load(buf)

comparison_cache = {}
if pathlib.Path(cache_fname).exists():
    with open(cache_fname, "r") as f:
        comparison_cache = json.load(f)

def best_permutation(reg: Region, template: Region, match, threshold, insensitive, resolve, r_template, to_discard, user_choices):
    print("Solving ...")
    maximize = match == "features"
    matcher = matchers[match]
    resolver = resolvers[resolve]
    perm = {}
    cache_key = f"region={reg.compute_data_hash()};template={template.compute_data_hash()};matcher={match}"
    cached = cache_key in comparison_cache
    if cached: matrix = str2array(comparison_cache[cache_key])
    else: matrix = np.empty((len(template.chunks), len(reg.chunks)), dtype=int)
    for m, xz in enumerate(template.chunks):
        if xz in to_discard: continue
        print(f"\r{int(m / len(template.chunks) * 100)}%", end="", flush=True)
        x_base = xz[0] * 16 + template.world_offset[0] * 512
        z_base = xz[1] * 16 + template.world_offset[1] * 512
        template_chunk = template.chunks[xz]
        r_template_chunk = template_chunk if r_template is None else r_template.chunks[xz]
        candidates = []
        best_metric = float("inf") * (1 - 2 * maximize)
        for n, i in enumerate(reg.chunks):
            if cached:
                metric = matrix[m, n]
            else:
                metric = matcher(reg.chunks[i], template_chunk)
                matrix[m, n] = metric
            best_metric = (max if maximize else min)(best_metric, metric)
            if (metric >= threshold) if maximize else (metric <= threshold):
                candidates.append({"metric": metric, "i": i})
        if len(candidates) == 0:
            print(f"\nNo candidates found for chunk at {xz} (block coordinates: x[{x_base}, {x_base + 16}], z[{z_base}, {z_base + 16}])")
            continue
        _candidates = candidates
        if insensitive: candidates = [c["i"] for c in candidates]
        else: candidates = [c["i"] for c in candidates if c["metric"] == best_metric]
        k = 0
        if len(candidates) > 1:
            print(f"\n{len(candidates)} candidates found for chunk at {xz} (block coordinates: x[{x_base}, {x_base + 16}], z[{z_base}, {z_base + 16}])")
            if xz in user_choices:
                k = user_choices[xz] % len(candidates)
                print(f"candidate {k} is chosen by user")
            else:
                remetrics = [resolver(reg.chunks[i], r_template_chunk) for i in candidates]
                m = min(remetrics)
                if remetrics.count(m) > 1:
                    print(f"WARNING: resolver was indecisive (candidates {", ".join(map(str, [j for j in range(len(remetrics)) if remetrics[j] == m]))} with the best resolver metric of {abs(m)}); choosing arbitrary")
                k = remetrics.index(m)
                print(f"candidate {k} is chosen")
        perm[xz] = candidates[k]
    if not cached:
        comparison_cache[cache_key] = array2str(matrix)
        with open(cache_fname, "w") as f:
            json.dump(comparison_cache, f)
    print("\ndone.")
    return perm

def parse_chunk_list(s):
    if s == "": return []
    assert re.fullmatch(r"[bc]\[-?[0-9]+,-?[0-9]+\](?:,[bc]\[-?[0-9]+,-?[0-9]+\])*", s), "Wrong chunk list format"
    chunks = []
    for name in re.findall(r"[bc]\[-?[0-9]+,-?[0-9]+\]", s):
        x, z = map(int, name[2:-1].split(","))
        if name[0] == "b":
            x //= 16
            z //= 16
        chunks.append((x % 32, z % 32))
    return chunks

def parse_choice_list(s):
    if s == "": return []
    assert re.fullmatch(r"[bc]\[-?[0-9]+,-?[0-9]+\]:-?[0-9]+(?:,[bc]\[-?[0-9]+,-?[0-9]+\]:-?[0-9]+)*", s), "Wrong choice list format"
    chunks = {}
    for name in re.findall(r"[bc]\[-?[0-9]+,-?[0-9]+\]:-?[0-9]+", s):
        x, z = map(int, name[2: name.index(":") - 1].split(","))
        choice = int(name.split(":")[1])
        if name[0] == "b":
            x //= 16
            z //= 16
        chunks[x % 32, z % 32] = choice
    return chunks

def main(args):
    print(f"Restoring {args.region} using {args.template} as template" + ("" if args.resolver_template is None else f" and {args.resolver_template} as resolver template"))
    region = Region(args.region)
    template = Region(args.template)
    print("Searching for intact chunks in the corrupted region ... ", end="", flush=True)
    n = region.parse_chunks_notable()
    print("done.")
    print(f"Found {n} chunks (a regular region contains 1024)")
    if n == 0:
        print("No chunks found; exiting")
        return
    print(f"Parsing template chunks ... ", end="", flush=True)
    template.parse_chunks_normal()
    print("done.")
    if len(template.chunks) == 0:
        print("Template contains no chunks; exiting")
        return
    if len(template.chunks) != 1024:
        print(f"WARNING: Template contains {len(template.chunks)} of 1024 chunk(s); consider fully loading and resaving the template region")

    if not args.resolver_template is None:
        r_template = Region(args.resolver_template)
        print(f"Parsing resolver template chunks ... ", end="", flush=True)
        r_template.parse_chunks_normal()
        print("done.")
        for xz in template.chunks:
            if xz not in r_template.chunks:
                x_base = xz[0] * 16 + template.world_offset[0] * 512
                z_base = xz[1] * 16 + template.world_offset[1] * 512
                print(
                    f"Chunk at {xz} (block coordinates: x[{x_base}, {x_base + 16}], z[{z_base}, {z_base + 16}]) is present in the template but not in resolver template; "
                    "resolver template should fully cover the main template; consider fully loading and resaving the resolver template region; exiting"
                )
                return

    if args.match == "features":
        print("Extracting features for template chunks ... ", end="", flush=True)
        template.extract_features()
        print("done.")
    if "features" in args.resolver:
        print("Extracting features for corrupted region chunks ... ", end="", flush=True)
        region.extract_features()
        print("done.")
    perm = best_permutation(region, template, args.match, args.threshold,
                            args.all_candidates, args.resolver, None if args.resolver_template is None else r_template,
                            parse_chunk_list(args.discard), parse_choice_list(args.choices))
    print(f"{len(perm)} ({int(len(perm) / 1024 * 100)}%) chunks restored")
    print("Applying permutation ... ", end="", flush=True)
    region.apply_permutation(perm)
    if args.fill:
        n = 0
        for xz in template.chunks:
            if xz not in region.chunks:
                n += 1
                region.chunks[xz] = template.chunks[xz]
    region.chunks2data()
    print("done.")
    if args.fill:
        print(f"{n} ({int(n / 1024 * 100)}%) chunks filled from template")
    region.save(args.output / args.region.name if args.output.is_dir() else args.output)

class SmartFormatter(argparse.HelpFormatter):
    def _split_lines(self, text, width):
        return sum([argparse.HelpFormatter._split_lines(self, line, width) for line in text.splitlines()], [])

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        prog="python mca_restorer.py",
        description="A script for fixing corrupted minecraft region files (.mca) that got misplaced chunks",
        formatter_class=SmartFormatter
    )
    parser.add_argument("region", type=pathlib.Path, help="the corrupted region file")
    parser.add_argument("template", type=pathlib.Path, help="the template region file for figuring out chunk positions")
    parser.add_argument("output", type=pathlib.Path, help="where to save the restored region")
    parser.add_argument("-m", "--match", choices=["bedrock", "features", "diff"], default="bedrock",
        help="the algorithm for matching chunks.\n"
             "`bedrock' counts mismatching bedrock blocks, ideal for overworld and nether when bedrock is intact, reasonable threshold value is 0 or however much bedrock you've changed;\n"
             "`features' is a smart algorithm that searches for matching block formations, the slowest, reasonable threshold value is 1000 (higher - harder to match);\n"
             "`diff' counts any block mismatches, the fastest, reasonable threshold value is (?) (lower - harder to match)\n"
             "default: `bedrock'")
    parser.add_argument("-t", "--threshold", type=int, default=0, help="numeric threshold to compare matching algorithm return value to; default: 0")
    parser.add_argument("-a", "--all-candidates", action="store_true",
        help="consider all chunks that matched below the threshold as candidates for resolver to choose from instead of only those that matched equally the best")
    parser.add_argument("-r", "--resolver", choices=["min-diff", "max-diff", "min-features", "max-features", "features-different", "features-similar"], default="max-diff",
        help="the algorithm for choosing between candidates in case of multiple matches (with --all-candidates) or multiple equally good best matches (without --all-candidates).\n"
             "`*-diff' checks block difference to the template, `max-diff' makes sense for choosing the more modified version over the one that minecraft might have regenerated after the corruption;\n"
             "`*-features' counts adjacent blocks of different types in the candidate chunks (more adjacent different blocks is considered more features);\n"
             "`features-*' uses same algorithm as `features' matching algorithm\n"
             "default: max-diff")
    parser.add_argument("-f", "--fill", action="store_true",
        help="use a chunk from template when no candidates are found for a location instead of leaving it out (which makes minecraft regenerate the chunk)")
    parser.add_argument("-T", "--resolver-template", type=pathlib.Path, help="a separate region file to use as a template for resolver rather than the main one (used for initial matching)")
    parser.add_argument("-d", "--discard", default="", help="list of chunks to discard in the format `<b|c>[<x>,<z>],<b|c>[<x>,<z>],...' (no whitespaces) where `b' means block coordinates and `c' means chunk coordinates; example: `b[1,2],c[0,-1]'")
    parser.add_argument("-c", "--choices", default="", help="list of user-defined resolving choices in the format `<b|c>[<x>,<z>]:<choice>,<b|c>[<x>,<z>]:<choice>,...' (no whitespaces) where `b' means block coordinates and `c' means chunk coordinates; example: `b[1,2]:0,c[0,-1]:1'")
    args = parser.parse_args()
    main(args)
