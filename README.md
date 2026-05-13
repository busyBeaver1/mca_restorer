## MCA restorer

This is a tool for restoring corrupted Minecraft block region files (`.mca` files in `region/`) when you have a world that started looking like hell of a mess of chunks after a crash. It helps when your world starts to look like this after your minecraft client or a server has crashed:

![corrupted world](https://i.imgur.com/upgBFHe.jpeg)

When the location table in the first sector of a region file (see [this wiki article](https://minecraft.wiki/w/Region_file_format)) gets misaligned with the actual chunk data all the chunks get out of place. If you then open the world Minecraft will also rewrite actual chunk locations stored internally in each chunk with no easy way to restore those.

This tool extracts all chunks it can find in the corrupted region file and matches them onto a template region which you can get from an old backup if you have one or by regenerating a world with the same seed as for the corrupted one. Basically the template region acts as a hint for where to place the chunks from the corrupted region and as a filler for chunks that are completely lost (if you provide --fill option). When encountering multiple candidates for a location by default it choses the one that is most block-different fron the template (presumably the one that got more modified is the newer / not fresh-regenerated version). For a detailed explanation see [this video](https://www.youtube.com/watch?v=HHCK_ub2jJA).

### Installation

First install [python](https://www.python.org/downloads/) and download the `mca_restorer.py` and `requirements` files. Go into the folder where you've placed those and run
```bash
python -m pip install -r requirements
```
to install the required libraries.

### Usage

Use
```bash
python mca_restorer.py --help
```
for an overview of the CLI.

First make a backup of your corrupted world. Then identify the corrupted regions. The corrupted ones should have the most recent modification timestamps in the folder. For each corrupted region get a template region either from an old backup of your world or by recreating a world with the same seed and fully loading the respectful regions. If you use a backup you should get a regenerated version to act as the resolver template anyways as it will act better with the `max-diff` resolver for choosing the candidate chunks that are farthest from the "natural" regenerated state. Note that you shold regenerate the world on the same version as where the corrupted regions were generated. Then run the tool as follows:

```bash
python mca_restorer.py path/to/corrupted/region path/to/template/region output/path [-T path/to/resolver/template]
```

If you've broken or placed some bedrock in the region, add the option `-t number-of-modified-blocks`. If you are restoring chunks for the end where there is no bedrock pattern you should use a different matching algorithm like `-m features -t 1000` (not tested in the end). Once you have the restored region create a creative superflat world and copy that region to the superflat world. Inspect the region in creative to see all the lost chunks and identify all chunks that seem out of place. For the latter ones look at the output of `mca_restorer` and see if there are different candidates for those chunks. If so, choose the different candidates by providing the `--choices`/`-c` option (see help message). If some chunks are still wrong and you'd prefer to use ones from a backup that you have as the template, provide those to the `--discard`/`-d` option. After all manual fixes if you have some chunks missing and you have a backup as the template then rerun the tool with the `--fill`/`-f` flag to fill the missing chunks from the template.

Repeat this process for all your corrupted regions. Then restore your corrupted world from the backup you've made and copy all the restored regions into the world. Enter the world with a low render distance because otherwise at least in my case corrupted entity regions made it hang indefinitely. On a low render distance the entity regions seem to fix themselves on their own.

Hope this helps :-)
