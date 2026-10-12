# Research source excerpt. Not the original operational deployment.
# See docs/CODE_CHANGELOG.md and src/README.md for omissions and dependencies.
"""Closed Statcast field routing. Unknown additions cannot silently disappear."""
DOC_URL = 'https://baseballsavant.mlb.com/csv-docs'
PHYSICS = {
    'release_extension':'ft', 'pfx_x':'ft, catcher perspective', 'pfx_z':'ft, catcher perspective',
    'release_pos_x':'ft', 'release_pos_y':'ft', 'release_pos_z':'ft',
    'vx0':'ft/s at y=50ft', 'vy0':'ft/s at y=50ft', 'vz0':'ft/s at y=50ft',
    'ax':'ft/s^2 at y=50ft', 'ay':'ft/s^2 at y=50ft', 'az':'ft/s^2 at y=50ft',
    'effective_speed':'mph, derived from extension', 'sz_top':'ft; 2026 ABS zone', 'sz_bot':'ft; 2026 ABS zone',
    'api_break_z_with_gravity':'provider vertical break including gravity; inspect reference before cross-source comparison',
    'api_break_x_arm':'provider raw unit unverified; arm side; no cross-source magnitude comparison',
    'api_break_x_batter_in':'provider raw unit unverified; toward batter; no cross-source magnitude comparison', 'arm_angle':'degrees'}
SWING = {'bat_speed':'mph; arithmetic mean of available rows, NOT official top-90% swing average',
    'swing_length':'ft', 'attack_angle':'degrees', 'attack_direction':'degrees; retain raw hand-specific coordinate convention',
    'swing_path_tilt':'degrees', 'intercept_ball_minus_batter_pos_x_inches':'inches',
    'intercept_ball_minus_batter_pos_y_inches':'inches'}
CONTACT = {'hit_distance_sc':'ft, projected', 'hc_x':'provider hit coordinate', 'hc_y':'provider hit coordinate',
    'estimated_ba_using_speedangle':'model estimate conditional on reported contact',
    'estimated_slg_using_speedangle':'model estimate conditional on reported contact',
    'hyper_speed':'mph, provider adjusted EV; not an independent event'}
BASE = {'game_pk','game_date','game_type','batter','pitcher','stand','p_throws','pitch_type','zone',
    'description','type','release_speed','release_spin_rate','plate_x','plate_z','launch_speed','launch_angle',
    'launch_speed_angle','estimated_woba_using_speedangle','spin_axis'}
CONTEXT = {'balls','strikes','outs_when_up','inning','inning_topbot','on_1b','on_2b','on_3b',
    'n_thruorder_pitcher','n_priorpa_thisgame_player_at_bat','if_fielding_alignment','of_fielding_alignment',
    'bb_type','hit_location','fielder_2','pitcher_days_since_prev_game','batter_days_since_prev_game'}
FUTURE = {'pitcher_days_until_next_game','batter_days_until_next_game'}
DEPRECATED = {'spin_dir','spin_rate_deprecated','break_angle_deprecated','break_length_deprecated',
    'tfs_deprecated','tfs_zulu_deprecated','umpire'}
ALIASES = {'player_name','game_year','pitch_name','sv_id','age_pit_legacy','age_bat_legacy','age_pit','age_bat'}
RAW_CONTEXT = {'at_bat_number','pitch_number','home_team','away_team','events','des',
    'fielder_3','fielder_4','fielder_5','fielder_6','fielder_7','fielder_8','fielder_9',
    'home_score','away_score','bat_score','fld_score','post_away_score','post_home_score','post_bat_score','post_fld_score',
    'home_score_diff','bat_score_diff','delta_home_win_exp','delta_run_exp','delta_pitcher_run_exp',
    'home_win_exp','bat_win_exp','woba_value','woba_denom','babip_value','iso_value'}
UNVERIFIED = {'miss_distance'}


def field_contract(names):
    groups=[('paired_base',BASE),('pitch_physics',set(PHYSICS)),('swing_mechanics',set(SWING)),
        ('contact_quality',set(CONTACT)),('historical_context',CONTEXT),('excluded_future',FUTURE),
        ('excluded_deprecated',DEPRECATED),('identity_or_alias_reference',ALIASES),
        ('historical_raw_reference',RAW_CONTEXT),('definition_unverified_reference',UNVERIFIED)]
    known=set()
    for _, fields in groups:
        assert not known & fields, 'Duplicate field routing'
        known |= fields
    assert set(names) <= known, 'Unclassified source fields: '+str(sorted(set(names)-known))
    rows=[]
    for name in names:
        group=next(g for g,s in groups if name in s)
        rows.append({'field':name,'route':group,'unit':PHYSICS.get(name,SWING.get(name,CONTACT.get(name))),
            'consumed_by_reducer':group in ('paired_base','pitch_physics','swing_mechanics','contact_quality','historical_context')})
    return {'source_documentation':DOC_URL,'fields':rows,'unclassified':[],
        'notes':['Historical context/outcomes are past-game descriptions, never target-game evidence.',
            'Raw-reference fields remain retrievable from the hash-bound source; not claimed as interpreted features.',
            'Missing measurements are not zero. Provider EV/launch angle can include estimates.',
            'Same event across PBP and Statcast is not independent support.',
            'miss_distance has no verified definition here; do not infer its meaning or score it.']}


def selected_columns(names):
    return [r['field'] for r in field_contract(names)['fields'] if r['consumed_by_reducer']]
