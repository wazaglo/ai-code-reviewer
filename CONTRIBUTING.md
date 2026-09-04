# Contributing to Code Reviewer

Thank you for your interest in contributing to the Code Reviewer project! This document provides a simple guide for getting started.

## Getting Started

1. Fork the repository
2. Create a new branch for your feature or bug fix
3. Make your changes
4. Run tests to ensure everything works
5. Submit a pull request

## Development Setup

1. Install required dependencies:
   ```bash
   pip install -r requirements-dev.txt
   ```

2. Install pre-commit hooks:
   ```bash
   pre-commit install
   ```

3. Run tests:
   ```bash
   pytest
   ```

## Code Style

- We use [Ruff](https://docs.astral.sh/ruff/) for linting and formatting
- Follow Python's PEP 8 style guidelines
- Write clear, descriptive commit messages
- Add tests for new functionality

## Pull Request Process

1. Update documentation as needed
2. Ensure all tests pass
3. At least one team member must review and approve
4. Once approved, your PR will be merged

## Questions?

Feel free to open an issue for any questions about contributing!
