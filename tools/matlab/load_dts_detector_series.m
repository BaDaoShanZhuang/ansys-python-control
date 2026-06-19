function series = load_dts_detector_series(dtsFile, outputFolder)
%LOAD_DTS_DETECTOR_SERIES  Read ansys-mechanical-zemax detector .dts time series.
%
%   series = LOAD_DTS_DETECTOR_SERIES(dtsFile) unzips the custom .dts
%   container (a ZIP archive), reads manifest.json and loads every frame's
%   full-resolution detector matrix plus metadata.
%
%   series = LOAD_DTS_DETECTOR_SERIES(dtsFile, outputFolder) unzips into a
%   chosen folder (default: <dts>_unzipped next to the file).
%
%   IMPORTANT: use the *_matlab_raw.dts file. It stores per-frame
%   detector.csv (numeric flux matrices). The plain playback .dts stores
%   detector.png images instead and cannot be loaded here.
%
%   The .dts (matlab_raw) ZIP layout is:
%       manifest.json
%       frames/0001/detector.csv     % full-resolution flux matrix
%       frames/0001/pose.json        % imported lens displacement/rotation
%       frames/0002/...
%
%   Returned struct 'series':
%       .manifest        decoded manifest.json (project, detector, schema, ...)
%       .detector        3-D array [Y x X x N], stacked frame matrices
%       .frames(k)       per-frame struct:
%           .frame_index, .time_value, .time_label
%           .detector    [Y x X] flux matrix (row = Y bottom-origin, col = X)
%           .centroid    [cx, cy] pixel centroid (from manifest, else computed)
%           .total_flux, .max_value, .summary, .pose
%       .centroids       [N x 2] centroid of every frame, in pixels
%       .outputFolder    where the archive was unzipped
%
%   Requires MATLAB R2019b+ (unzip, jsondecode, readmatrix).
%
%   Example:
%       s = load_dts_detector_series('result_matlab_raw.dts');
%       imagesc(s.frames(1).detector); axis image; colorbar
%       plot(s.centroids(:,1), s.centroids(:,2), '-o')   % centroid trajectory

    narginchk(1, 2);
    if isempty(dtsFile) || exist(dtsFile, 'file') ~= 2
        error('load_dts_detector_series:notFound', 'DTS file not found: %s', string(dtsFile));
    end
    if nargin < 2 || isempty(outputFolder)
        [parentFolder, baseName, ~] = fileparts(dtsFile);
        if isempty(parentFolder)
            parentFolder = pwd;
        end
        outputFolder = fullfile(parentFolder, [baseName '_unzipped']);
    end
    if ~exist(outputFolder, 'dir')
        mkdir(outputFolder);
    end

    unzip(dtsFile, outputFolder);

    manifestPath = fullfile(outputFolder, 'manifest.json');
    if exist(manifestPath, 'file') ~= 2
        error('load_dts_detector_series:noManifest', ...
            'manifest.json not found inside %s', dtsFile);
    end
    manifest = jsondecode(fileread(manifestPath));

    % manifest.frames decodes to a struct array (homogeneous) OR a cell
    % array (heterogeneous, e.g. different imported_objects per frame).
    rawFrames = manifest.frames;
    if iscell(rawFrames)
        nFrames = numel(rawFrames);
        getFrame = @(k) rawFrames{k};
    else
        nFrames = numel(rawFrames);
        getFrame = @(k) rawFrames(k);
    end
    if nFrames == 0
        error('load_dts_detector_series:noFrames', 'manifest has no frames.');
    end

    template = struct('frame_index', {}, 'time_value', {}, 'time_label', {}, ...
                      'detector', {}, 'centroid', {}, 'total_flux', {}, ...
                      'max_value', {}, 'summary', {}, 'pose', {});
    frames = template;
    detector3d = [];
    centroids = nan(nFrames, 2);

    for k = 1:nFrames
        f = getFrame(k);

        csvRel = local_get(f, 'detector_csv', '');
        if isempty(csvRel)
            error('load_dts_detector_series:noCsv', ...
                ['Frame %d has no detector_csv field. ' ...
                 'Open the *_matlab_raw.dts file, not the playback .dts.'], k);
        end
        csvPath = fullfile(outputFolder, strrep(char(csvRel), '/', filesep));
        if exist(csvPath, 'file') ~= 2
            error('load_dts_detector_series:csvMissing', ...
                'detector.csv missing for frame %d: %s', k, csvPath);
        end
        M = readmatrix(csvPath);

        frames(k).frame_index = local_get(f, 'frame_index', k);
        frames(k).time_value  = local_get(f, 'time_value', []);
        frames(k).time_label  = char(local_get(f, 'time_label', ''));
        frames(k).detector    = M;

        s = local_get(f, 'summary', struct());
        cx = local_get(s, 'centroid_x_pixel', NaN);
        cy = local_get(s, 'centroid_y_pixel', NaN);
        if ~(isscalar(cx) && isfinite(cx) && isscalar(cy) && isfinite(cy))
            [cx, cy] = local_centroid(M);   % fallback: compute from matrix
        end
        frames(k).centroid   = [cx, cy];
        frames(k).total_flux = local_get(s, 'total_flux', sum(M(:)));
        frames(k).max_value  = local_get(s, 'max_value',  max(M(:)));
        frames(k).summary    = s;
        centroids(k, :)      = [cx, cy];

        poseRel = local_get(f, 'pose_json', '');
        posePath = '';
        if ~isempty(poseRel)
            posePath = fullfile(outputFolder, strrep(char(poseRel), '/', filesep));
        end
        if ~isempty(posePath) && exist(posePath, 'file') == 2
            frames(k).pose = jsondecode(fileread(posePath));
        else
            frames(k).pose = struct();
        end

        if isempty(detector3d)
            detector3d = zeros(size(M, 1), size(M, 2), nFrames);
        end
        detector3d(:, :, k) = M;
    end

    series = struct();
    series.manifest     = manifest;
    series.frames       = frames;
    series.detector     = detector3d;
    series.centroids    = centroids;
    series.outputFolder = outputFolder;
end


function v = local_get(s, name, default)
%LOCAL_GET  Return s.(name) if present and non-empty, else default.
    if isstruct(s) && isfield(s, name) && ~isempty(s.(name))
        v = s.(name);
    else
        v = default;
    end
end


function [cx, cy] = local_centroid(M)
%LOCAL_CENTROID  Flux-weighted centroid in pixels (row = Y, col = X, 0-based).
    [ny, nx] = size(M);
    [X, Y] = meshgrid(0:nx - 1, 0:ny - 1);
    total = sum(M(:));
    if total <= 0
        cx = NaN; cy = NaN; return;
    end
    cx = sum(sum(M .* X)) / total;
    cy = sum(sum(M .* Y)) / total;
end
