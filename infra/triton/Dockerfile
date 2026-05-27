# Triton image with sentence-transformers preinstalled so the Python
# backend can serve bge-m3 out of the box.
#
# We build on top of the official Triton 24.06 image. It already ships
# Python 3 + the python_backend stub; we just add the embedding stack.
#
# CPU torch keeps the image lean and lets it run on machines without
# GPUs (Ryzen 7, dev laptops). On a GPU host you can swap the index
# URL for cu121 / cu124 wheels, or rebuild from the larger
# tritonserver:24.06-py3 image with CUDA torch.
FROM nvcr.io/nvidia/tritonserver:24.06-py3-cpu

# Triton's python_backend uses /usr/bin/python3 (the system interpreter).
# Install our deps there. We pin torch CPU explicitly so pip doesn't
# pull the CUDA build by mistake.
RUN python3 -m pip install --no-cache-dir --upgrade pip \
    && python3 -m pip install --no-cache-dir \
        --extra-index-url https://download.pytorch.org/whl/cpu \
        torch==2.3.1 \
    && python3 -m pip install --no-cache-dir \
        sentence-transformers==3.0.1 \
        transformers==4.41.2 \
        huggingface-hub==0.23.4

# Pre-create the HF cache dir so a mounted volume has the right
# ownership.
RUN mkdir -p /root/.cache/huggingface

# The actual command comes from docker-compose; this is just a default.
CMD ["tritonserver", "--model-repository=/models", "--http-port=8001", \
     "--strict-model-config=false"]
