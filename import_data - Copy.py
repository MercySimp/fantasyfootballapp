import nflreadpy as nfl
df = nfl.load_player_stats(seasons=[2023]).to_pandas()
print([c for c in df.columns if "team" in c.lower()])