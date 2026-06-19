function dts_demo(dtsFile, savePng)
%DTS_DEMO  Load a detector .dts time series and print + plot a summary.
%
%   dts_demo('result_matlab_raw.dts')
%   dts_demo('result_matlab_raw.dts', 'out.png')   % also save a figure
%
%   Prints per-frame total flux / peak / centroid, and the centroid jitter
%   RMS across frames (a quick line-of-sight jitter estimate).

    narginchk(1, 2);
    if nargin < 2
        savePng = '';
    end

    series = load_dts_detector_series(dtsFile);
    n = numel(series.frames);
    [ny, nx] = size(series.frames(1).detector);

    fprintf('==== DTS summary ====\n');
    fprintf('File      : %s\n', dtsFile);
    fprintf('Schema    : %s\n', get_or(series.manifest, 'schema', '(none)'));
    fprintf('Project   : %s\n', get_or(series.manifest, 'system_name', '(none)'));
    fprintf('Frames    : %d\n', n);
    fprintf('Detector  : %d x %d (Y x X)\n', ny, nx);

    C = series.centroids;                       % N x 2 pixels
    meanC = mean(C, 1, 'omitnan');
    rmsX = std(C(:, 1), 1, 'omitnan');
    rmsY = std(C(:, 2), 1, 'omitnan');
    fprintf('Centroid mean (px) : X=%.3f  Y=%.3f\n', meanC(1), meanC(2));
    fprintf('Centroid range (px): X=[%.3f, %.3f]  Y=[%.3f, %.3f]\n', ...
        min(C(:, 1)), max(C(:, 1)), min(C(:, 2)), max(C(:, 2)));
    fprintf('Centroid jitter RMS (px): X=%.4g  Y=%.4g  radius=%.4g\n', ...
        rmsX, rmsY, hypot(rmsX, rmsY));

    fprintf('\nFrame | time_label | total_flux |   max    | centroid(x,y) px\n');
    for k = 1:min(n, 8)
        fr = series.frames(k);
        fprintf('%4d  | %-10s | %10.4g | %8.4g | (%.3f, %.3f)\n', ...
            fr.frame_index, fr.time_label, fr.total_flux, fr.max_value, ...
            fr.centroid(1), fr.centroid(2));
    end
    if n > 8
        fprintf('  ... (%d more frames)\n', n - 8);
    end

    if ~isempty(savePng)
        fig = figure('Visible', 'off', 'Position', [100 100 940 420]);
        subplot(1, 2, 1);
        imagesc(series.frames(1).detector); axis image; set(gca, 'YDir', 'normal');
        colorbar; title('frame 1 detector (flux)');
        xlabel('X (px)'); ylabel('Y (px)');
        subplot(1, 2, 2);
        plot(C(:, 1), C(:, 2), '-o', 'MarkerSize', 4); axis equal; grid on;
        xlabel('centroid X (px)'); ylabel('centroid Y (px)');
        title('centroid trajectory across frames');
        exportgraphics(fig, savePng, 'Resolution', 120);
        close(fig);
        fprintf('\nSaved figure: %s\n', savePng);
    end
end


function v = get_or(s, name, default)
    if isstruct(s) && isfield(s, name) && ~isempty(s.(name))
        v = s.(name);
    else
        v = default;
    end
end
